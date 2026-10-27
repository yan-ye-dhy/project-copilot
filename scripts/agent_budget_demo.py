"""用受控循环观察实际LangGraph预算拒绝；不调用模型、数据库或工具后端。"""

import argparse
import asyncio
from uuid import uuid4

from langchain_core.messages import AIMessage

from project_copilot.agent.graph import AgentRunner, GraphLimits
from project_copilot.agent.tools import ToolRegistry
from project_copilot.schemas.agent import AgentQuestion


class ControlledLoop:
    async def ainvoke(self, messages, tools):
        return AIMessage(content="", tool_calls=[{
            "id": "call_" + uuid4().hex, "type": "tool_call",
            "name": "execute_sql", "args": {"sql": "forbidden"},
        }])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local", action="store_true")
    args = parser.parse_args()
    if not args.local:
        parser.error("本地受控演示需显式添加--local")
    runner = AgentRunner(ControlledLoop(), ToolRegistry(), GraphLimits(max_model_calls=2, max_tool_calls=2))
    result = asyncio.run(runner.run(AgentQuestion(question="受控未知工具循环"), run_id=str(uuid4())))
    print(result.model_dump_json(indent=2))
    return 0 if result.status == "budget_exhausted" and result.tool_executions == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
