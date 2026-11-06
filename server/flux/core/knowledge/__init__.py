"""助理知识库（RAG）：对话 / 文档 / 项目记忆的增量索引与混合检索。"""

from flux.core.knowledge.chunker import ConversationMessage, TextChunk
from flux.core.knowledge.embedding import EmbeddingClient
from flux.core.knowledge.indexer import BRAIN, CONVERSATION, DOC, KnowledgeIndexer
from flux.core.knowledge.repository import KnowledgeRepository, PreparedChunk
from flux.core.knowledge.retriever import KnowledgeHit, KnowledgeRetriever
from flux.core.knowledge.worker import KnowledgeWorker

__all__ = [
    "BRAIN",
    "CONVERSATION",
    "DOC",
    "ConversationMessage",
    "EmbeddingClient",
    "KnowledgeHit",
    "KnowledgeIndexer",
    "KnowledgeRepository",
    "KnowledgeRetriever",
    "KnowledgeWorker",
    "PreparedChunk",
    "TextChunk",
]
