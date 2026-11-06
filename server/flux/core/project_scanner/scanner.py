"""Project Scanner（主规格 §5.8；实施计划 ⑩）。

只做**静态画像**：读目录结构与常见清单文件，产出一个可机器消费的 `ProjectProfile`。
不调用模型、不落库、不写被扫描的项目——扫描必须是无副作用的只读操作。

三条边界：

1. **有界遍历**：文件数与目录深度都有上限（来自 `FLUX_PROJECT_SCAN_*`），触顶时
   画像照常产出但 `truncated=True`，绝不为了"扫全"而把时间/内存打满。
2. **不猜语义**：只报告能证明的事实（文件后缀、清单里的依赖名、锁文件）。
   项目"是做什么的"由人来写，Scanner 不编故事。
3. **不引入新依赖**：只用标准库 + 已有 `GitClient`；不解析到 TOML 语法层，
   对清单只做"是否出现某个依赖名"的判定（Python 3.10 无 tomllib）。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flux.core.git_integration.client import GitClient
from flux.core.virtual_workspace.apply_engine import resolve_workspace_root
from flux.errors import NotAGitRepositoryError
from flux.logging import get_logger

logger = get_logger(__name__)

# 依赖清单与常见配置：画像里统一放进 manifests（相对路径，按字典序）
MANIFEST_NAMES = frozenset(
    {
        "pyproject.toml",
        "requirements.txt",
        "requirements-dev.txt",
        "requirements-test.txt",
        "setup.py",
        "setup.cfg",
        "Pipfile",
        "package.json",
        "go.mod",
        "Cargo.toml",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "Gemfile",
        "composer.json",
        "uv.lock",
        "poetry.lock",
        "pnpm-lock.yaml",
        "yarn.lock",
        "bun.lock",
        "bun.lockb",
        "package-lock.json",
    }
)
CONFIG_NAMES = frozenset(
    {
        "Dockerfile",
        "docker-compose.yml",
        "docker-compose.yaml",
        "Makefile",
        "tsconfig.json",
        "ruff.toml",
        ".ruff.toml",
        "pytest.ini",
        "tox.ini",
        ".flake8",
        "mypy.ini",
        ".pre-commit-config.yaml",
        ".editorconfig",
        "README.md",
        "README.rst",
        "README.txt",
        "README",
    }
)

# 目录级噪声：既不进语言统计，也不出现在顶层结构里
IGNORED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".flux",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".tox",
        ".idea",
        ".vscode",
        "dist",
        "build",
        ".next",
        ".nuxt",
        ".cache",
        "coverage",
        "target",
    }
)

# 只有这些后缀算"语言"；markup / 配置（.md/.json/.yaml/.html/.css）不计入
EXTENSION_LANGUAGES = {
    ".py": "Python",
    ".pyi": "Python",
    ".js": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".jsx": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".vue": "Vue",
    ".svelte": "Svelte",
    ".java": "Java",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".go": "Go",
    ".rs": "Rust",
    ".rb": "Ruby",
    ".php": "PHP",
    ".cs": "C#",
    ".c": "C",
    ".h": "C",
    ".cpp": "C++",
    ".cc": "C++",
    ".hpp": "C++",
    ".swift": "Swift",
    ".scala": "Scala",
    ".sh": "Shell",
    ".ps1": "PowerShell",
    ".sql": "SQL",
    ".ipynb": "Jupyter",
}

# 依赖名 → 展示名。只收"能说明技术栈"的运行时/应用框架与 SDK
PYTHON_FRAMEWORKS = {
    "fastapi": "FastAPI",
    "django": "Django",
    "flask": "Flask",
    "starlette": "Starlette",
    "litestar": "Litestar",
    "tornado": "Tornado",
    "aiohttp": "aiohttp",
    "sanic": "Sanic",
    "streamlit": "Streamlit",
    "gradio": "Gradio",
    "scrapy": "Scrapy",
    "celery": "Celery",
    "sqlalchemy": "SQLAlchemy",
    "pydantic": "Pydantic",
    "torch": "PyTorch",
    "tensorflow": "TensorFlow",
    "transformers": "Transformers",
    "jax": "JAX",
    "langchain": "LangChain",
    "openai": "OpenAI SDK",
    "anthropic": "Anthropic SDK",
    "pandas": "pandas",
    "numpy": "NumPy",
    "alembic": "Alembic",
}

NODE_FRAMEWORKS = {
    "react": "React",
    "next": "Next.js",
    "vue": "Vue",
    "nuxt": "Nuxt",
    "svelte": "Svelte",
    "@sveltejs/kit": "SvelteKit",
    "@angular/core": "Angular",
    "express": "Express",
    "koa": "Koa",
    "@nestjs/core": "NestJS",
    "fastify": "Fastify",
    "electron": "Electron",
    "vite": "Vite",
    "webpack": "Webpack",
    "typescript": "TypeScript",
    "tailwindcss": "Tailwind CSS",
    "three": "Three.js",
    "socket.io": "Socket.IO",
    "prisma": "Prisma",
}

ENTRY_POINT_NAMES = frozenset(
    {
        "main.py",
        "app.py",
        "server.py",
        "manage.py",
        "cli.py",
        "run.py",
        "wsgi.py",
        "asgi.py",
        "__main__.py",
        "index.js",
        "index.ts",
        "index.mjs",
        "index.cjs",
        "main.js",
        "main.ts",
        "server.js",
        "server.ts",
        "app.js",
        "app.ts",
    }
)

# 清单读取上限：画像只需要判断"用没用某个依赖"，不读巨型锁文件
MAX_MANIFEST_BYTES = 200_000
MAX_ENTRY_POINTS = 10
MAX_COMMANDS = 5
MAX_STRUCTURE_ENTRIES = 50
# 清单与入口文件的搜索深度（根目录 + 一层子目录，覆盖 backend/、apps/web/ 这类布局）
MANIFEST_MAX_DEPTH = 2
ENTRY_POINT_MAX_DEPTH = 3

# 目标名必须以字母或下划线开头：这样 `\tpytest`（配方行，以制表符开头）与
# `.PHONY:`（特殊目标，以点开头）都不会被误当成可执行目标
_MAKEFILE_TARGET_PATTERN = re.compile(r"^([A-Za-z_][A-Za-z0-9_.-]*)\s*:(?!=)", re.MULTILINE)


@dataclass(frozen=True)
class ProjectProfile:
    """项目画像（实施计划 ⑩ 的 Project Profile）。"""

    root: str
    languages: tuple[str, ...]
    primary_language: str | None
    frameworks: tuple[str, ...]
    package_manager: str | None
    manifests: tuple[str, ...]
    entry_points: tuple[str, ...]
    test_commands: tuple[str, ...]
    build_commands: tuple[str, ...]
    git_repository: bool
    git_branch: str | None
    structure: tuple[str, ...]
    files_scanned: int
    truncated: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "languages": list(self.languages),
            "primary_language": self.primary_language,
            "frameworks": list(self.frameworks),
            "package_manager": self.package_manager,
            "manifests": list(self.manifests),
            "entry_points": list(self.entry_points),
            "test_commands": list(self.test_commands),
            "build_commands": list(self.build_commands),
            "git_repository": self.git_repository,
            "git_branch": self.git_branch,
            "structure": list(self.structure),
            "files_scanned": self.files_scanned,
            "truncated": self.truncated,
        }


def _dep_present(text: str, name: str) -> bool:
    """清单里是否出现某个依赖名（前后不能紧邻 `-` 或单词字符，避免误判子串）。"""
    return re.search(rf"(?<![\w.-]){re.escape(name)}(?![\w.-])", text) is not None


def _read_text(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def parse_makefile_targets(text: str) -> set[str]:
    """解析 Makefile 的目标名（只认顶格写的 `target:`，跳过配方行与 `.PHONY`）。"""
    return {match.group(1) for match in _MAKEFILE_TARGET_PATTERN.finditer(text)}


class ProjectScanner:
    """把一棵目录树读成 `ProjectProfile`。"""

    def __init__(
        self,
        *,
        git: GitClient | None = None,
        max_files: int = 2000,
        max_depth: int = 6,
    ) -> None:
        self._git = git
        self._max_files = max_files
        self._max_depth = max_depth

    def scan(self, *, workspace_root: str | Path | None = None) -> ProjectProfile:
        root = resolve_workspace_root(workspace_root)
        files, truncated = self._collect_files(root)
        relative = [path.relative_to(root) for path in files]
        relative_str = [path.as_posix() for path in relative]

        language_counts: dict[str, int] = {}
        for path in relative:
            language = EXTENSION_LANGUAGES.get(path.suffix.lower())
            if language:
                language_counts[language] = language_counts.get(language, 0) + 1
        languages = tuple(sorted(language_counts, key=lambda name: (-language_counts[name], name)))
        primary = languages[0] if languages else None

        manifests = self._detect_named_files(relative_str, MANIFEST_NAMES | CONFIG_NAMES)
        manifest_texts = self._read_manifests(root, relative_str)
        package_manager = self._detect_package_manager(manifest_texts, primary)
        is_git, branch = self._detect_git(root)

        return ProjectProfile(
            root=str(root),
            languages=languages,
            primary_language=primary,
            frameworks=self._detect_frameworks(manifest_texts),
            package_manager=package_manager,
            manifests=manifests,
            entry_points=self._detect_entry_points(root, relative_str, manifest_texts),
            test_commands=self._detect_test_commands(manifest_texts, relative_str, primary),
            build_commands=self._detect_build_commands(
                manifest_texts, relative_str, primary, root.name
            ),
            git_repository=is_git,
            git_branch=branch,
            structure=self._structure(root),
            files_scanned=len(files),
            truncated=truncated,
        )

    # --- 遍历 ---

    def _collect_files(self, root: Path) -> tuple[list[Path], bool]:
        """按深度优先收集文件；触到文件数上限或深度上限即停并标记 truncated。"""
        files: list[Path] = []
        truncated = False
        for current, dirnames, filenames in os.walk(root):
            current_path = Path(current)
            depth = len(current_path.relative_to(root).parts)
            dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
            if depth >= self._max_depth:
                # 还有子目录但不再下探：结果确实被裁剪了
                if dirnames:
                    truncated = True
                dirnames[:] = []
            for name in sorted(filenames):
                if len(files) >= self._max_files:
                    return files, True
                files.append(current_path / name)
        return files, truncated

    def _structure(self, root: Path) -> tuple[str, ...]:
        entries: list[str] = []
        for entry in sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name)):
            if entry.name in IGNORED_DIRS:
                continue
            entries.append(f"{entry.name}/" if entry.is_dir() else entry.name)
        return tuple(entries[:MAX_STRUCTURE_ENTRIES])

    # --- 清单 ---

    @staticmethod
    def _detect_named_files(relative: list[str], names: frozenset[str]) -> tuple[str, ...]:
        found = [
            path
            for path in relative
            if Path(path).name in names and len(Path(path).parts) <= MANIFEST_MAX_DEPTH
        ]
        return tuple(sorted(found))

    @staticmethod
    def _read_manifests(root: Path, relative: list[str]) -> dict[str, str]:
        """读取根目录与一层子目录下的清单内容（键为相对路径）。"""
        texts: dict[str, str] = {}
        for path in relative:
            parts = Path(path).parts
            if len(parts) > MANIFEST_MAX_DEPTH or parts[-1] not in MANIFEST_NAMES | CONFIG_NAMES:
                continue
            text = _read_text(root / path)
            if text is not None:
                texts[path] = text
        return texts

    def _detect_package_manager(
        self, manifests: dict[str, str], primary_language: str | None
    ) -> str | None:
        names = {Path(path).name for path in manifests}
        if primary_language in {"JavaScript", "TypeScript", "Vue", "Svelte"}:
            return self._node_package_manager(names)
        if "uv.lock" in names:
            return "uv"
        if any("[tool.poetry]" in text for text in manifests.values()):
            return "poetry"
        if "Pipfile" in names or "Pipfile.lock" in names:
            return "pipenv"
        if {"pyproject.toml", "requirements.txt", "setup.py", "setup.cfg"} & names:
            return "pip"
        # 没有 Python 文件但项目里有 Node 清单时，仍按 Node 生态给出包管理器
        if "package.json" in names:
            return self._node_package_manager(names)
        return None

    @staticmethod
    def _node_package_manager(names: set[str]) -> str:
        for lockfile, manager in (
            ("pnpm-lock.yaml", "pnpm"),
            ("yarn.lock", "yarn"),
            ("bun.lockb", "bun"),
            ("bun.lock", "bun"),
            ("package-lock.json", "npm"),
        ):
            if lockfile in names:
                return manager
        return "npm"  # package.json 在但没锁文件：npm 是默认

    def _detect_frameworks(self, manifests: dict[str, str]) -> tuple[str, ...]:
        found: list[str] = []
        python_text = "\n".join(
            text
            for path, text in manifests.items()
            if Path(path).name in {"pyproject.toml", "requirements.txt", "setup.py", "Pipfile"}
        ).lower()
        if python_text:
            found += [
                display
                for name, display in PYTHON_FRAMEWORKS.items()
                if _dep_present(python_text, name)
            ]
        for path, text in manifests.items():
            if Path(path).name != "package.json":
                continue
            dependencies = self._node_dependencies(text)
            found += [display for name, display in NODE_FRAMEWORKS.items() if name in dependencies]
        return tuple(dict.fromkeys(found))

    @staticmethod
    def _node_dependencies(text: str) -> set[str]:
        try:
            package = json.loads(text)
        except json.JSONDecodeError:
            return set()
        if not isinstance(package, dict):
            return set()
        names: set[str] = set()
        for key in ("dependencies", "devDependencies", "peerDependencies"):
            section = package.get(key)
            if isinstance(section, dict):
                names.update(str(name) for name in section)
        return names

    @staticmethod
    def _node_scripts(text: str) -> dict[str, str]:
        try:
            package = json.loads(text)
        except json.JSONDecodeError:
            return {}
        if not isinstance(package, dict):
            return {}
        scripts = package.get("scripts")
        return {str(k): str(v) for k, v in scripts.items()} if isinstance(scripts, dict) else {}

    # --- 入口 / 命令 ---

    def _detect_entry_points(
        self, root: Path, relative: list[str], manifests: dict[str, str]
    ) -> tuple[str, ...]:
        candidates = [
            path
            for path in relative
            if Path(path).name in ENTRY_POINT_NAMES
            and len(Path(path).parts) <= ENTRY_POINT_MAX_DEPTH
        ]
        for path, text in manifests.items():
            if Path(path).name != "package.json":
                continue
            try:
                package = json.loads(text)
            except json.JSONDecodeError:
                continue
            if not isinstance(package, dict):
                continue
            main = package.get("main") or package.get("module")
            if isinstance(main, str) and (root / main).is_file():
                candidates.append(main)
        return tuple(sorted(dict.fromkeys(candidates))[:MAX_ENTRY_POINTS])

    @staticmethod
    def _node_packages(manifests: dict[str, str]) -> list[str]:
        """所有 package.json 的文本（命令探测只看 Node 清单，不去猜别的文件）。"""
        return [text for path, text in manifests.items() if Path(path).name == "package.json"]

    @staticmethod
    def _detect_test_commands(
        manifests: dict[str, str], relative: list[str], primary_language: str | None
    ) -> tuple[str, ...]:
        commands: list[str] = []
        names = {Path(path).name for path in manifests}
        python_text = "\n".join(
            text
            for path, text in manifests.items()
            if Path(path).name in {"pyproject.toml", "requirements.txt", "setup.py", "Pipfile"}
        ).lower()
        has_python_files = any(Path(path).suffix in {".py", ".pyi"} for path in relative)
        has_python_tests = has_python_files and any(
            Path(path).name.startswith("test_") or Path(path).parent.name == "tests"
            for path in relative
        )
        if "django" in python_text:
            commands.append("python manage.py test")
        if (
            _dep_present(python_text, "pytest")
            or any(name in {"pytest.ini", "tox.ini"} for name in names)
            or has_python_tests
        ):
            commands.append("pytest")
        if "go.mod" in names:
            commands.append("go test ./...")
        if "Cargo.toml" in names:
            commands.append("cargo test")
        if primary_language in {"JavaScript", "TypeScript", "Vue", "Svelte"}:
            for text in ProjectScanner._node_packages(manifests):
                if "test" in ProjectScanner._node_scripts(text):
                    commands.append(f"{ProjectScanner._node_package_manager(names)} run test")
                    break
        for path, text in manifests.items():
            if Path(path).name == "Makefile" and "test" in parse_makefile_targets(text):
                commands.append("make test")
                break
        return tuple(dict.fromkeys(commands))[:MAX_COMMANDS]

    @staticmethod
    def _detect_build_commands(
        manifests: dict[str, str],
        relative: list[str],
        primary_language: str | None,
        root_name: str,
    ) -> tuple[str, ...]:
        commands: list[str] = []
        names = {Path(path).name for path in manifests}
        if primary_language in {"JavaScript", "TypeScript", "Vue", "Svelte"}:
            for text in ProjectScanner._node_packages(manifests):
                if "build" in ProjectScanner._node_scripts(text):
                    commands.append(f"{ProjectScanner._node_package_manager(names)} run build")
                    break
        if "go.mod" in names:
            commands.append("go build ./...")
        if "Cargo.toml" in names:
            commands.append("cargo build")
        if any("[build-system]" in text for text in manifests.values()):
            commands.append("python -m build")
        for path, text in manifests.items():
            if Path(path).name == "Makefile" and "build" in parse_makefile_targets(text):
                commands.append("make build")
                break
        if any(Path(path).name == "Dockerfile" for path in relative):
            commands.append(f"docker build -t {_image_name(root_name)} .")
        return tuple(dict.fromkeys(commands))[:MAX_COMMANDS]

    # --- Git ---

    def _detect_git(self, root: Path) -> tuple[bool, str | None]:
        """是否 Git 仓库 + 当前分支。

        交给 ⑨ 的 `GitClient`（与 Apply / Git 提交同一个根），不自己解析 `.git/HEAD`。
        git 二进制缺失属于环境故障，`GitError` 直接抛出，不降级成"不是仓库"。
        """
        if self._git is None:
            return (root / ".git").exists(), None
        try:
            status = self._git.status(workspace_root=root)
        except NotAGitRepositoryError:
            return False, None
        return True, status.branch


def _image_name(root_name: str) -> str:
    """把目录名规整成合法的 Docker 镜像名（小写、非字母数字一律换成 `-`）。"""
    slug = re.sub(r"[^a-z0-9]+", "-", root_name.lower()).strip("-")
    return slug or "app"
