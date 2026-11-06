"""上下文机制的核心持久化模型（FLUX_CONTEXT_MECHANISM §3.3 / §3.5）。

一份只追加的 step 事件日志 + 一个内容寻址的原文库：

- `context_steps`：一条 step = 一次工具调用 / 命令 / 模型调用 / 文件操作 / 断言 / 事实。
  它是压缩、检索、原子事实的**最小单元**。`run_id` 管归属（交接 / 信任链），
  `turn_id` 管呈现与删除（删会话即删该会话所有 turn 的 step）——两把钥匙不可互推。
- `context_blobs`：原文按 `sha256` 内容寻址存放，同一份文件 / 日志 / 命令输出**天然去重**；
  存储层再用 zstd（缺失时退 lzma）**无损**压缩。去重与压缩都不改变原文，
  故与 I2（原文满保真）不冲突——I3 说的"不压缩"只指语义不压缩。
- `context_step_tokens`：反向索引词条（§7.2），把"文件 / 命令 / URL / 错误关键字"这类**线索**
  映射回 `step_id`——正向 `fetch` 需要 id，而接管 agent 手里往往只有线索。
- `context_verifications`：验证账本（§4.4），一行 = 一次"某指纹在某类断言上被验证"。
  它是**派生数据**（记的是别人验证的结论，不是原文），故随会话一并删除（§8.2）。
- `context_redactions`：脱敏映射表（§10.1），占位符 → **被换下来的原文**，值单独加密。
  它本身敏感（存的就是从正文里换下来的秘密），故随会话一并删除（§8.2 / §13-5）。

主键形态（**有意偏离 §11.5**）：step 设计上要求 `step_id` 全局单调有序（§3.3），
目录行要能写 `#0142`，因此本表用**自增整数**主键，与 `agent_runs` 用非 UUID 主键同一性质。
验证账本同样需要"按发生顺序排"才能判出连胜与最近一次，故也走自增整数（见该类文档）。
契约测试只校验"存在名为 `id` 的主键 + created_at / updated_at"，两表都满足。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, BigInteger, Boolean, Integer, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from flux.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

#: SQLite 只有 INTEGER 主键才自增，故对 sqlite 方言降级为 Integer；其它方言保留 BigInteger。
_STEP_ID_TYPE = BigInteger().with_variant(Integer, "sqlite")


class ContextStep(TimestampMixin, Base):
    """一条 step（§3.3）——压缩 / 检索 / 原子事实的最小单元，只追加不改写。

    `created_at` 即规范里的 `ts`（时间戳），不另立列。
    """

    __tablename__ = "context_steps"

    #: step_id：全局单调有序，目录行直接引用它（`#0142`）
    id: Mapped[int] = mapped_column(_STEP_ID_TYPE, primary_key=True, autoincrement=True)
    #: 归属单元：交接 / 信任链跟它走（agent_runs.id 的字符串形态，此处不设外键，
    #: 删除与分区由 §8.2 的级联逻辑显式处理）
    run_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    #: 呈现与删除单元：删会话即删该会话所有 turn 的 step
    turn_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    #: 该 step 在所属 turn 内的序号
    seq_in_turn: Mapped[int] = mapped_column(Integer, default=0)
    #: 取值见 flux.enums.StepKind
    kind: Mapped[str] = mapped_column(String(16), index=True)
    #: 指纹 `agent_id:model_name`（§4.1），接管 agent 一眼看出上一步是谁、用什么模型做的
    agent_fp: Mapped[str] = mapped_column(String(160), index=True, default="")
    #: 工具名（kind=tool_call 时有值）
    tool: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 命令参数摘要（kind=command 时有值，用于同命令识别与去重判定）
    argv_digest: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: 主目标（文件路径 / URL / 用例名），**反向索引的锚**（§7.2）
    target_ref: Mapped[str | None] = mapped_column(String(1024), index=True, nullable=True)
    #: 取值见 flux.enums.StepStatus；只有 ok 才算"这一步做成了"
    status: Mapped[str] = mapped_column(String(16), index=True)
    #: 命令退出码（有则填）
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 结果原文的内容寻址 hash（指向 context_blobs.sha256），fetch 取件的钥匙
    result_sha256: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    #: 结果原文的原始字节数
    result_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    #: 结果原文的**预览头**（目录行内联的"事实最小锚"，让不取件也能核对）；
    #: 完整原文在内容寻址库，不在这列——否则 CAS 去重就白做了
    result_head: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 结构化事实数组（§4.3）
    facts: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    #: 该 step 是断言且尚未被验证时置位（§4.2）
    unverified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.id,
            "run_id": self.run_id,
            "turn_id": self.turn_id,
            "seq_in_turn": self.seq_in_turn,
            "ts": self.created_at.isoformat() if self.created_at else None,
            "kind": self.kind,
            "agent_fp": self.agent_fp,
            "tool": self.tool,
            "argv_digest": self.argv_digest,
            "target_ref": self.target_ref,
            "status": self.status,
            "exit_code": self.exit_code,
            "result_sha256": self.result_sha256,
            "result_bytes": self.result_bytes,
            "result_head": self.result_head,
            "facts": self.facts,
            "unverified": bool(self.unverified),
        }


class ContextBlob(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """内容寻址的原文库（§3.5）——同一段原文只存一份，存储层无损压缩。

    `codec` 记录**实际**用的编码（`raw` / `zstd` / `lzma`），解码一律按它取值，
    不看长度猜；三种编码都是逐字节可还原的。
    """

    __tablename__ = "context_blobs"

    #: 原文的 sha256（hex），内容寻址的钥匙，也是去重的判据
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    #: 取值见 flux.enums.BlobCodec
    codec: Mapped[str] = mapped_column(String(16))
    #: 编码前的原始字节数（还原后应与此相等）
    size_raw: Mapped[int] = mapped_column(BigInteger)
    #: 落库的实际字节数（压缩率 = size_raw / size_stored）
    size_stored: Mapped[int] = mapped_column(BigInteger)
    #: 编码后的原文
    data: Mapped[bytes] = mapped_column(LargeBinary)

    def to_dict(self) -> dict[str, Any]:
        return {
            "blob_id": str(self.id),
            "sha256": self.sha256,
            "codec": self.codec,
            "size_raw": self.size_raw,
            "size_stored": self.size_stored,
        }


class ContextStepToken(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """反向索引词条（§7.2）——把 step 的线索（文件 / 命令 / URL / 错误关键字）映射回 step。

    正向 `fetch(step_id)` 的前提是 agent **已经知道 id**；但接管 agent 手里通常只有线索
    （"我记得有人改过 auth.py，但不知道是哪一段"）。没有这张表，回看通道就是通的但没人走得进去。

    按功能拆成独立行而非塞进 `context_steps` 的 JSON 列：只有这样 `token` 才能建普通索引，
    五类查询里最常用的"按文件 / 按命令"才不会退化成全表 LIKE。

    `step_id` / `run_id` / `turn_id` 都冗余在这儿（不设外键）：插入时一次写好，
    删除与按 run 过滤都不必回 join `context_steps`——与 §8.2 的显式级联同一取舍。
    """

    __tablename__ = "context_step_tokens"

    #: 指向 context_steps.id（不自增，仅索引；级联由 §8.2 的删除逻辑显式处理）
    step_id: Mapped[int] = mapped_column(_STEP_ID_TYPE, index=True)
    #: 冗余的归属键，按 run 过滤时免 join
    run_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    #: 冗余的删除键，删会话时免 join
    turn_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    #: 取值见 flux.enums.ContextTokenKind
    kind: Mapped[str] = mapped_column(String(16), index=True)
    #: 归一化后的词条（小写；文件同时收全路径与 basename，URL 收域名）
    token: Mapped[str] = mapped_column(String(512), index=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "token_id": str(self.id),
            "step_id": self.step_id,
            "run_id": self.run_id,
            "turn_id": self.turn_id,
            "kind": self.kind,
            "token": self.token,
        }


class ContextVerification(TimestampMixin, Base):
    """验证账本（§4.4）——一行 = 一次"某指纹在某类断言上被验证"。

    账本粒度是**指纹 × 断言类**（§4.4）：同一个 agent 在"命令"上被证伪，不该连累它
    在"文件 hash"上的信用。故 `agent_fp` 与 `claim_kind` 两列都建索引，判级按这对键取记录。

    **主键同为自增整数**（与 `context_steps` 一样的偏离，理由更硬）：分级要判的是"最近一次
    是过是败、连胜多长"，因此账本**必须**能按发生顺序排。`created_at` 在 SQLite 上只到秒
    （`CURRENT_TIMESTAMP`），同一秒内记的多条会并列而排不出先后；换个单调主键，
    "最近一次"才有唯一答案，分级才不是掷骰子。

    它是**派生数据**：记的是"别人验证了什么结论"，不是原文，所以压缩 / 快照那一套全不适用，
    只需按 §8.2 随会话级联删除（故带 `run_id` / `turn_id`，与 step / token 同一把删除钥匙）。
    删除会话后本表必须归零（§13-5），否则"删干净了"就是假的。

    `note` 只记**证伪理由**（为什么判 FAIL），不记通过理由——通过无需解释，证伪才要留痕。
    """

    __tablename__ = "context_verifications"

    #: 自增主键：账本的发生顺序就是它的顺序（见类文档）
    id: Mapped[int] = mapped_column(_STEP_ID_TYPE, primary_key=True, autoincrement=True)
    #: 指纹 `agent_id:model_name`（§4.1），信任分级的对象
    agent_fp: Mapped[str] = mapped_column(String(160), index=True, default="")
    #: 取值见 flux.enums.ClaimKind，账本粒度的另一半
    claim_kind: Mapped[str] = mapped_column(String(16), index=True)
    #: 取值见 flux.enums.VerificationOutcome
    outcome: Mapped[str] = mapped_column(String(16), index=True)
    #: 归属单元：交接 / 信任链跟它走（与 ContextStep 同义）
    run_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    #: 删除单元：删会话即删该会话所有 turn 的验证记录
    turn_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    #: 作为证据的那条 step（可空：账本允许先记结论、后补证据）
    step_id: Mapped[int | None] = mapped_column(_STEP_ID_TYPE, index=True, nullable=True)
    #: 证伪理由（仅 FAIL 时有值；通过不留痕）
    note: Mapped[str | None] = mapped_column(String(256), nullable=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verification_id": self.id,
            "agent_fp": self.agent_fp,
            "claim_kind": self.claim_kind,
            "outcome": self.outcome,
            "run_id": self.run_id,
            "turn_id": self.turn_id,
            "step_id": self.step_id,
            "note": self.note,
            "ts": self.created_at.isoformat() if self.created_at else None,
        }


class ContextRedaction(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """脱敏映射表（§10.1）——`placeholder → 原文`，值**单独加密**。

    入站即脱敏（§10.1）：正文先打码再存档，被换下来的秘密另存本表。这张表**本身敏感**
    ——存的正是"从正文里换下来的原文"，一旦跟打码后的正文一起裸奔，打码就白做了。
    故 `ciphertext` 由 `core/context/secretbox.py` 的信封加密，密钥来自 `FLUX_REDACTION_KEY`；
    无密钥时**不落库**（fail-closed）：正文照常打码，只是不可回填。

    它随会话级联删除（§8.2 / §13-5）：故带 `run_id` / `turn_id`，与 step / token 同一把删除钥匙。
    删除会话后本表必须归零——否则"删干净了"就是假的，且残留的还是秘密。

    占位符只在 **turn 内**唯一（跨 turn 会重名），故回填必须同时带 `turn_id`。
    """

    __tablename__ = "context_redactions"

    #: 归属单元：交接 / 信任链跟它走（与 ContextStep 同义）
    run_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    #: 删除单元：删会话即删该会话所有 turn 的脱敏映射
    turn_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    #: 取值见 flux.enums.RedactionKind
    kind: Mapped[str] = mapped_column(String(16), index=True)
    #: 正文里的占位符（如 `[REDACTED:key]`），回填的钥匙
    placeholder: Mapped[str] = mapped_column(String(64), index=True)
    #: 被换下来的原文，**加密存放**（信封见 core/context/secretbox.py）
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)

    def to_dict(self) -> dict[str, Any]:
        """刻意**不回显密文与明文**：这张表的形态信息本身就是敏感的，只给元数据。"""
        return {
            "redaction_id": str(self.id),
            "run_id": self.run_id,
            "turn_id": self.turn_id,
            "kind": self.kind,
            "placeholder": self.placeholder,
            "ciphertext_bytes": len(self.ciphertext),
        }
