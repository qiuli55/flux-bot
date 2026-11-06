"""投喂压缩流水线（FLUX_CONTEXT_MECHANISM §5）。

§5.1 的立场决定这里**没有模型**：压缩 = 选择 + 编码，不是生成问题。
五段流水线（§5.3）逐段落在这里：

| 段 | 做什么 | 模块 |
| --- | --- | --- |
| S1 | 符号表字典编码（`@1 = path`，无损可逆） | `symbols` |
| S2 | 结构抽取（终端 / 日志 / diff / 文件 / 结构化数据） | `extract` |
| S3 | 抽取式筛选（BM25 + MMR，选中即原文逐字照抄） | `select` |
| S4 | 分级装配（必留区先扣预算，其余按等级填） | `context.budget` + `context_packager` |
| S5 | 自校验（不一致则整段降级为 ref：宁可不压，不可压错） | `verify` |
"""

from flux.core.context.compress.extract import (
    ContentForm,
    Extraction,
    classify,
    extract,
)
from flux.core.context.compress.select import Selection, select, split_sentences
from flux.core.context.compress.symbols import SymbolTable, build_symbol_table
from flux.core.context.compress.verify import Verification, verify

__all__ = [
    "ContentForm",
    "Extraction",
    "Selection",
    "SymbolTable",
    "Verification",
    "build_symbol_table",
    "classify",
    "extract",
    "select",
    "split_sentences",
    "verify",
]
