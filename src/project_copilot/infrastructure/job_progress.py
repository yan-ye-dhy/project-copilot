"""Optional execution observer. No task execution or credentials live in events."""
from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Callable

_observer: ContextVar[Callable[[str, dict], None] | None] = ContextVar("job_progress", default=None)

# Labels are backend-owned; never interpolate model arguments, results or errors.
LABELS = {
    "running": "任务开始执行", "retrieval": "正在检索知识库", "rerank": "正在重排知识片段",
    "model": "正在整理回答", "get_ticket": "正在查询工单", "search_tickets": "正在检索相关工单",
    "search_knowledge": "正在检索知识库", "propose_ticket": "正在生成待确认提案",
    "tool": "正在执行工具", "tool_done": "工具执行结束", "validate": "正在校验回答与来源",
    "draining": "正在等待已启动的工具执行结束", "finalizing": "正在核实执行结果并保存任务状态",
}


def active() -> bool:
    return _observer.get() is not None


def emit(kind: str, data: dict) -> None:
    observer = _observer.get()
    if observer is not None:
        observer(kind, data)


def progress(stage: str) -> None:
    emit("progress", {"stage": stage, "label": LABELS[stage]})


@contextmanager
def observe(observer):
    token = _observer.set(observer)
    try:
        yield
    finally:
        _observer.reset(token)
