"""Per-execution checkpoint context; receipts never confer business authority."""
from contextlib import contextmanager
from contextvars import ContextVar
import time
from uuid import UUID

from sqlalchemy import func, select

from project_copilot.services.job_steps import StepJournal, StepRejected, fingerprint

_current = ContextVar("copilot_recovery_runtime", default=None)


def current_recovery():
    return _current.get()


def _manifest(settings):
    from project_copilot.rag.ingestion import ManifestStore
    from project_copilot.rag.splitters import SplitConfig
    from project_copilot.schemas.retrieval import DenseIndexSpec
    return ManifestStore(settings.manifest_dir, DenseIndexSpec(
        collection_name=settings.qdrant_collection_name, embedding_model=settings.embedding_model,
        dimensions=settings.embedding_expected_dimensions, splitter_version=SplitConfig().index_version))


def checkpoint_bindings(settings):
    """Cheap atomic-file bindings: never wait for an index lock inside a PG lock."""
    from project_copilot.services.release_control import config_fingerprint
    try:
        manifest = _manifest(settings).load()
        return config_fingerprint(settings), fingerprint(manifest.model_dump(mode="json"))
    except Exception:
        raise StepRejected("bindings") from None


def _sources(value):
    """Read bounded normalized server receipts, never parse model text as evidence."""
    found = []
    def visit(item, depth=0):
        if depth > 32:
            raise StepRejected("result_contract")
        if isinstance(item, dict):
            if set(("id", "kind", "data")) <= item.keys() and item.get("kind") in {"knowledge", "ticket", "proposal"}:
                found.append(item)
            for child in item.values():
                visit(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
    visit(value)
    return found


class RecoveryRuntime:
    def __init__(self, database, settings, runtime_settings, job, *, deadline_remaining):
        self.database, self.settings, self.runtime_settings, self.job = database, settings, runtime_settings, job
        self.config_sha256, self.index_fingerprint = checkpoint_bindings(settings)
        self._deadline = time.monotonic() + deadline_remaining
        self._prepared = False
        self._knowledge = {}
        self.journal = StepJournal(database, settings, job.id, job.execution_lease.token,
            config_sha256=self.config_sha256, index_fingerprint=self.index_fingerprint,
            revalidate=self._revalidate)

    def remaining(self):
        return max(0.0, self._deadline - time.monotonic())

    def validate_generation(self):
        if checkpoint_bindings(self.settings) != (self.config_sha256, self.index_fingerprint):
            raise StepRejected("metadata_mismatch")

    def prepare(self):
        self.validate_generation()
        if self._prepared:
            return
        from project_copilot.db.recovery import AiJobStep
        from project_copilot.services.execution_slots import guard_execution
        from project_copilot.services.job_recovery import revalidate_job_authority
        with self.database.transaction() as session:
            revalidate_job_authority(session, self.settings, self.job.id)
            guard_execution(session, self.job.id, self.job.execution_lease.token)
            receipts = session.scalars(select(AiJobStep.result).where(
                AiJobStep.job_id == self.job.id, AiJobStep.status == "completed")).all()
        if any(source["kind"] == "knowledge" for result in receipts for source in _sources(result)):
            # Network/index/model validation happens outside ALL PG transactions.
            # Replaying a receipt must not bypass the ordinary completed-index
            # consistency or fixed local-reranker availability checks.
            from project_copilot.rag.final_retrieval import get_final_retriever
            generation, chunks = get_final_retriever(self.runtime_settings).checkpoint_view()
            if generation != self.index_fingerprint:
                raise StepRejected("metadata_mismatch")
            self._knowledge = chunks
        self._prepared = True

    def _revalidate(self, session, kind, result):
        self.validate_generation()
        from project_copilot.services.job_recovery import revalidate_job_authority
        from project_copilot.services.auth import AuthService
        from project_copilot.services.tickets import TicketService
        from project_copilot.services.approvals import ApprovalService
        from project_copilot.schemas.retrieval import RetrievedChunk
        job, _ = revalidate_job_authority(session, self.settings, self.job.id)
        principal, _ = AuthService(session).authenticate_session_id(job.login_session_id)
        try:
            for source in _sources(result):
                namespace, identity = source["id"].split(":", 1)
                identity = UUID(identity)
                if namespace != source["kind"]:
                    return False
                if namespace == "knowledge":
                    cached = RetrievedChunk.model_validate(source["data"])
                    actual = self._knowledge.get(identity)
                    if actual is None or cached.text != actual.text or cached.metadata != actual.metadata:
                        return False
                elif namespace == "ticket":
                    actual = TicketService(session, principal=principal, enforce_ownership=True).get(identity)
                    if actual.model_dump(mode="json") != source["data"]:
                        return False
                else:
                    if job.context_id is None:
                        return False
                    service = ApprovalService(session, principal=principal, enforce_ownership=True)
                    context = service.authenticate_owned(job.context_id)
                    actual = service.get(context, identity)
                    if actual.model_dump(mode="json") != source["data"]:
                        return False
        except (ValueError, TypeError, LookupError, KeyError):
            return False
        return True

    def recover_proposal(self):
        """Restore the original backend action, without asking a model to recreate it."""
        if self.job.kind != "agent" or self.job.attempt_no < 2:
            return None
        from project_copilot.db.recovery import JobBusinessOperation, AiJobRecoveryState, AiJobStep
        from project_copilot.services.execution_slots import guard_execution, _business_valid
        from project_copilot.services.job_recovery import revalidate_job_authority
        from project_copilot.services.auth import AuthService
        from project_copilot.services.approvals import ApprovalService
        from project_copilot.schemas.agent import AgentRun, AgentSource
        with self.database.transaction() as session:
            job, reservation = revalidate_job_authority(session, self.settings, self.job.id)
            proof = guard_execution(session, self.job.id, self.job.execution_lease.token)
            operation = session.get(JobBusinessOperation, (self.job.id, "propose_ticket"))
            if operation is None:
                return None
            if not _business_valid(session, reservation, proof):
                raise StepRejected("business_binding")
            principal, _ = AuthService(session).authenticate_session_id(job.login_session_id)
            service = ApprovalService(session, principal=principal, enforce_ownership=True)
            context = service.authenticate_owned(job.context_id)
            action = service.get(context, operation.action_id)
            data = action.model_dump(mode="json")
            state = session.get(AiJobRecoveryState, self.job.id)
            receipts = session.scalars(select(AiJobStep.result).where(AiJobStep.job_id == self.job.id,
                AiJobStep.kind == "tool", AiJobStep.status == "completed")).all()
            known_executions = sum(bool(row.get("data", {}).get("executed")) for row in receipts)
            proposal_receipted = any(any(source.get("id") == "proposal:" + str(action.action_id)
                for source in row.get("sources", [])) for row in receipts)
            known_executions += int(not proposal_receipted)
            known_failures = max((row.get("data", {}).get("failures", 0) for row in receipts), default=0)
            return AgentRun(run_id=str(job.id), status="pending_confirmation",
                answer="已恢复原工单提案；请查看当前状态。确认或拒绝仍须由你独立操作。",
                sources=[AgentSource(id="proposal:" + str(action.action_id), kind="proposal", data=data)],
                events=[], model_calls=state.model_calls, tool_calls=state.tool_calls,
                tool_executions=known_executions, tool_failures=known_failures,
                usage=reservation.usage, model_events=[], elapsed_ms=0, failure_type=None)


@contextmanager
def recovery_scope(database, settings, runtime_settings, job):
    if not getattr(settings, "job_recovery_enabled", False) or job.execution_lease is None:
        yield None
        return
    from project_copilot.db.recovery import AiJobRecoveryState
    from project_copilot.services.job_recovery import revalidate_job_authority
    with database.transaction() as session:
        revalidate_job_authority(session, settings, job.id)
        state = session.get(AiJobRecoveryState, job.id)
        if state is None:
            runtime = None
        else:
            now = session.scalar(select(func.clock_timestamp()))
            remaining = (state.deadline_at - now).total_seconds() if state.deadline_at else 0
            runtime = True
    if runtime is None:
        yield None
        return
    runtime = RecoveryRuntime(database, settings, runtime_settings, job, deadline_remaining=remaining)
    token = _current.set(runtime)
    try:
        yield runtime
    finally:
        _current.reset(token)
