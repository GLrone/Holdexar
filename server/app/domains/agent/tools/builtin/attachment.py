"""附件工具：按行号区间读取本轮/本会话的文本附件。

长文本不走消息体（见 `app/domains/pilot/attachments.py` 模块说明）：消息里只有
引用，模型要看内容时用它按区间取。读取有两道闸——单次块预算（超出即收窄并
**显式告知截断**，模型据此续读）与附件归属（只能读本会话的附件，sid 由执行器
注入，模型无从指定别处路径）。"""
from __future__ import annotations

from app.domains.agent.tools.base import ToolSpec

_EMPTY_NOTE = "本会话没有这个附件：id 可能写错，或该附件属于另一段对话。"


async def read_attachment(aid: str, start_line: int | None = None,
                          end_line: int | None = None,
                          sid: str | None = None) -> dict:
    """读附件区间；附件缺失返回 empty（不抛错，模型自愈）。"""
    from app.domains.pilot import attachments as pilot_attachments

    if not sid or not pilot_attachments.valid_aid(str(aid or "")):
        return {"kind": "empty", "note": "bad_request"}
    got = pilot_attachments.read_range(
        sid, str(aid),
        start_line=int(start_line or 1),
        end_line=int(end_line) if end_line else None,
    )
    if got is None:
        return {"kind": "empty", "note": _EMPTY_NOTE}
    return got


async def _read_attachment(args: dict, sid: str | None = None) -> dict:
    return await read_attachment(
        str(args.get("id") or ""),
        args.get("start_line"),
        args.get("end_line"),
        sid=sid,
    )


SPECS = [
    ToolSpec(
        name="read_attachment", group="read", risk="low",
        description="读取用户本轮附带的文本附件（对话里以【附件 N】引用形式给出）。"
                    "附件是长文本，按行号区间读取：start_line 从 1 开始，不传 end_line 读到"
                    "单次上限为止。结果里 end_line 小于你要的区间时表示被截断，"
                    "接着用 start_line=end_line+1 继续读。与附件相关的问题应先读再答",
        parameters={"type": "object", "properties": {
            "id": {"type": "string", "description": "附件 id（引用里的 id=…）"},
            "start_line": {"type": "integer", "description": "起始行号（从 1 开始，默认 1）"},
            "end_line": {"type": "integer", "description": "结束行号（含该行；缺省读到长度上限）"},
        }, "required": ["id"]},
        handler=_read_attachment, step_label="attachmentRead", timeout_s=10.0,
    ),
]

# 注：timeout_s 由 builtin/__init__ 统一以 projection.budget_for 覆盖（20s 默认档），
# 此处声明值不作为运行时预算，仅保持声明完整。
