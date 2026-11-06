"""语言服务器清单与探测。

这里只描述「有哪些服务器、装什么命令、能不能跑」，不负责启动进程——
生命周期在 `service.py`，帧编解码在 `rpc.py`。

设计约束：

1. **不硬编码绝对路径**：只记录可执行文件名（如 `pyright-langserver`），
   探测时在 `PATH` 与常见用户级 bin 目录里找（`shutil.which`）。
2. **安装不静默执行**：`install` 只是候选命令行，必须由用户在前端显式触发，
   并且只会执行本文件里写死的命令，绝不拼接前端传入的任意参数。
3. **顺序即优先级**：同一种语言有多个服务器时，排在前面的先被选中。
4. **纯静态**：模块导入不触碰文件系统，`describe_servers()` 才会做探测。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# --- 文件后缀 → LSP languageId ------------------------------------------------
#
# languageId 必须与语言服务器、Monaco 的内置 language id 对齐，
# 否则 `didOpen` 会因为语言不识别而静默不返回任何能力。
EXTENSION_LANGUAGES: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".pyw": "python",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "typescriptreact",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascriptreact",
    ".json": "json",
    ".jsonc": "jsonc",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".scss": "scss",
    ".sass": "sass",
    ".less": "less",
    ".md": "markdown",
    ".markdown": "markdown",
    ".mdx": "mdx",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".sh": "shellscript",
    ".bash": "shellscript",
    ".zsh": "shellscript",
    ".ksh": "shellscript",
    ".ps1": "powershell",
    ".psm1": "powershell",
    ".dockerfile": "dockerfile",
    ".go": "go",
    ".rs": "rust",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".c++": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
    ".hh": "cpp",
    ".m": "objective-c",
    ".mm": "objective-cpp",
    ".lua": "lua",
    ".php": "php",
    ".rb": "ruby",
    ".rake": "ruby",
    ".gemspec": "ruby",
    ".java": "java",
    ".cs": "csharp",
    ".fs": "fsharp",
    ".fsi": "fsharp",
    ".fsx": "fsharp",
    ".vb": "vb",
    ".sql": "sql",
    ".vue": "vue",
    ".svelte": "svelte",
    ".zig": "zig",
    ".prisma": "prisma",
    ".dart": "dart",
    ".hs": "haskell",
    ".lhs": "haskell",
    ".ex": "elixir",
    ".exs": "elixir",
    ".erl": "erlang",
    ".hrl": "erlang",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".swift": "swift",
    ".tf": "terraform",
    ".tfvars": "terraform",
    ".typ": "typst",
    ".nix": "nix",
    ".sol": "solidity",
    ".cmake": "cmake",
    ".tex": "latex",
    ".bib": "bibtex",
    ".graphql": "graphql",
    ".gql": "graphql",
    ".proto": "proto",
    ".r": "r",
    ".jl": "julia",
    ".scala": "scala",
    ".sc": "scala",
    ".clj": "clojure",
    ".cljs": "clojure",
    ".cljc": "clojure",
    ".edn": "clojure",
    ".pl": "perl",
    ".pm": "perl",
    ".groovy": "groovy",
    ".xml": "xml",
    ".xsl": "xml",
    ".svg": "xml",
    ".ini": "ini",
    ".cfg": "ini",
}

#: 无后缀 / 特殊文件名 → languageId
FILENAME_LANGUAGES: dict[str, str] = {
    "dockerfile": "dockerfile",
    "containerfile": "dockerfile",
    "makefile": "makefile",
    "gnumakefile": "makefile",
    "cmakelists.txt": "cmake",
    "gemfile": "ruby",
    "rakefile": "ruby",
    "podfile": "ruby",
    "go.mod": "go",
    "go.sum": "go",
}

#: languageId → 展示名（前端语言选择器与「未安装」提示用）
LANGUAGE_LABELS: dict[str, str] = {
    "python": "Python",
    "typescript": "TypeScript",
    "typescriptreact": "TypeScript React",
    "javascript": "JavaScript",
    "javascriptreact": "JavaScript React",
    "json": "JSON",
    "jsonc": "JSON with Comments",
    "html": "HTML",
    "css": "CSS",
    "scss": "SCSS",
    "sass": "Sass",
    "less": "Less",
    "markdown": "Markdown",
    "mdx": "MDX",
    "yaml": "YAML",
    "toml": "TOML",
    "shellscript": "Shell",
    "powershell": "PowerShell",
    "dockerfile": "Dockerfile",
    "go": "Go",
    "rust": "Rust",
    "c": "C",
    "cpp": "C++",
    "objective-c": "Objective-C",
    "objective-cpp": "Objective-C++",
    "lua": "Lua",
    "php": "PHP",
    "ruby": "Ruby",
    "java": "Java",
    "csharp": "C#",
    "fsharp": "F#",
    "vb": "Visual Basic",
    "sql": "SQL",
    "vue": "Vue",
    "svelte": "Svelte",
    "zig": "Zig",
    "prisma": "Prisma",
    "dart": "Dart",
    "haskell": "Haskell",
    "elixir": "Elixir",
    "erlang": "Erlang",
    "kotlin": "Kotlin",
    "swift": "Swift",
    "terraform": "Terraform",
    "typst": "Typst",
    "nix": "Nix",
    "solidity": "Solidity",
    "cmake": "CMake",
    "latex": "LaTeX",
    "bibtex": "BibTeX",
    "graphql": "GraphQL",
    "proto": "Protocol Buffers",
    "r": "R",
    "julia": "Julia",
    "scala": "Scala",
    "clojure": "Clojure",
    "perl": "Perl",
    "groovy": "Groovy",
    "xml": "XML",
    "ini": "INI",
    "makefile": "Makefile",
}


@dataclass(frozen=True)
class LanguageServerSpec:
    """一个语言服务器的静态描述。"""

    id: str
    #: 展示名
    name: str
    #: 支持的 languageId（第一个视为该服务器的主语言）
    languages: tuple[str, ...]
    #: 启动命令；第一个元素是可执行文件名
    command: tuple[str, ...]
    #: 安装命令（可选）；仅在用户显式点击安装时执行
    install: tuple[str, ...] | None
    #: 安装说明（无法自动安装时给用户看）
    install_hint: str
    description: str
    homepage: str

    @property
    def executable_name(self) -> str:
        return self.command[0]

    def executable(self) -> str | None:
        """在 PATH 与常见 bin 目录里找到可执行文件；找不到返回 None。"""

        name = self.executable_name
        if os.path.isabs(name):
            return name if Path(name).is_file() else None
        found = shutil.which(name)
        if found:
            return found
        for directory in _extra_bin_dirs():
            candidate = directory / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        return None

    def available(self) -> bool:
        return self.executable() is not None

    def initialization_options(self, workspace_root: str | Path) -> dict[str, object] | None:
        """服务端补的 `initializationOptions`。

        TypeScript 服务器默认在项目里找 `node_modules/typescript`，项目没装依赖时
        直接拒绝启动；这里回退到全局 TypeScript，保证空仓库里也有成员补全。
        """

        if self.id != "typescript":
            return None
        local = Path(workspace_root) / "node_modules" / "typescript" / "lib" / "tsserver.js"
        tsserver = local if local.is_file() else _global_tsserver()
        if tsserver is None:
            return None
        return {"tsserver": {"path": str(tsserver)}}

    def describe(self) -> dict[str, object]:
        path = self.executable()
        return {
            "id": self.id,
            "name": self.name,
            "languages": list(self.languages),
            "command": list(self.command),
            "install": list(self.install) if self.install else None,
            "install_hint": self.install_hint,
            "description": self.description,
            "homepage": self.homepage,
            "available": path is not None,
            "executable": path,
        }


@lru_cache(maxsize=1)
def _extra_bin_dirs() -> tuple[Path, ...]:
    """用户级安装目录：pip --user / npm -g / cargo / go / local。"""

    home = Path.home()
    candidates = [
        home / ".local" / "bin",
        home / ".npm-global" / "bin",
        home / ".cargo" / "bin",
        home / "go" / "bin",
        home / ".bun" / "bin",
        Path("/usr/local/bin"),
        Path("/opt/homebrew/bin"),
    ]
    return tuple(path for path in candidates if path.is_dir())


@lru_cache(maxsize=1)
def _global_tsserver() -> Path | None:
    """全局安装的 TypeScript `tsserver.js`；用于项目没装依赖时的回退。"""

    roots: list[Path] = []
    executable = shutil.which("typescript-language-server")
    if executable:
        for parent in Path(executable).resolve().parents:
            if parent.name == "node_modules":
                roots.append(parent)
                break
    for directory in _extra_bin_dirs():
        roots.append(directory.parent / "lib" / "node_modules")
    for root in roots:
        candidate = root / "typescript" / "lib" / "tsserver.js"
        if candidate.is_file():
            return candidate
    return None


# --- 服务器清单 ---------------------------------------------------------------
#
# 顺序即优先级。npm 类服务器统一用 vscode-langservers-extracted 提供的 CLI。
SERVERS: tuple[LanguageServerSpec, ...] = (
    LanguageServerSpec(
        id="pyright",
        name="Pyright",
        languages=("python",),
        command=("pyright-langserver", "--stdio"),
        install=("npm", "install", "-g", "pyright"),
        install_hint="需要 Node.js；也可 pip install pyright",
        description="微软官方 Python 类型检查器，补全与类型推断最准。",
        homepage="https://github.com/microsoft/pyright",
    ),
    LanguageServerSpec(
        id="pylsp",
        name="Python LSP Server",
        languages=("python",),
        command=("pylsp",),
        install=("pip", "install", "python-lsp-server"),
        install_hint="pip install python-lsp-server",
        description="社区维护的 Python LSP，插件生态丰富，纯 Python 实现。",
        homepage="https://github.com/python-lsp/python-lsp-server",
    ),
    LanguageServerSpec(
        id="typescript",
        name="TypeScript Language Server",
        languages=("typescript", "typescriptreact", "javascript", "javascriptreact"),
        command=("typescript-language-server", "--stdio"),
        install=("npm", "install", "-g", "typescript", "typescript-language-server"),
        install_hint="npm install -g typescript typescript-language-server",
        description="TypeScript/JavaScript 官方语义能力，含 JSX/TSX。",
        homepage="https://github.com/typescript-language-server/typescript-language-server",
    ),
    LanguageServerSpec(
        id="vscode-html",
        name="HTML Language Server",
        languages=("html",),
        command=("vscode-html-language-server", "--stdio"),
        install=("npm", "install", "-g", "vscode-langservers-extracted"),
        install_hint="npm install -g vscode-langservers-extracted",
        description="VS Code 同款 HTML 服务器：标签/属性补全、Emmet 支持。",
        homepage="https://github.com/hrsh7th/vscode-langservers-extracted",
    ),
    LanguageServerSpec(
        id="vscode-css",
        name="CSS Language Server",
        languages=("css", "scss", "sass", "less"),
        command=("vscode-css-language-server", "--stdio"),
        install=("npm", "install", "-g", "vscode-langservers-extracted"),
        install_hint="npm install -g vscode-langservers-extracted",
        description="VS Code 同款 CSS/SCSS/Less 服务器。",
        homepage="https://github.com/hrsh7th/vscode-langservers-extracted",
    ),
    LanguageServerSpec(
        id="vscode-json",
        name="JSON Language Server",
        languages=("json", "jsonc"),
        command=("vscode-json-language-server", "--stdio"),
        install=("npm", "install", "-g", "vscode-langservers-extracted"),
        install_hint="npm install -g vscode-langservers-extracted",
        description="VS Code 同款 JSON 服务器：Schema 校验与补全。",
        homepage="https://github.com/hrsh7th/vscode-langservers-extracted",
    ),
    LanguageServerSpec(
        id="yaml",
        name="YAML Language Server",
        languages=("yaml",),
        command=("yaml-language-server", "--stdio"),
        install=("npm", "install", "-g", "yaml-language-server"),
        install_hint="npm install -g yaml-language-server",
        description="YAML Schema 校验、Kubernetes/GitHub Actions 补全。",
        homepage="https://github.com/redhat-developer/yaml-language-server",
    ),
    LanguageServerSpec(
        id="bash",
        name="Bash Language Server",
        languages=("shellscript",),
        command=("bash-language-server", "start"),
        install=("npm", "install", "-g", "bash-language-server"),
        install_hint="npm install -g bash-language-server",
        description="Shell 脚本补全与 shellcheck 诊断。",
        homepage="https://github.com/bash-lsp/bash-language-server",
    ),
    LanguageServerSpec(
        id="dockerfile",
        name="Dockerfile Language Server",
        languages=("dockerfile",),
        command=("docker-langserver", "--stdio"),
        install=("npm", "install", "-g", "dockerfile-language-server-nodejs"),
        install_hint="npm install -g dockerfile-language-server-nodejs",
        description="Dockerfile 指令补全与校验。",
        homepage="https://github.com/rcjsuen/dockerfile-language-server",
    ),
    LanguageServerSpec(
        id="gopls",
        name="gopls",
        languages=("go",),
        command=("gopls",),
        install=("go", "install", "golang.org/x/tools/gopls@latest"),
        install_hint="go install golang.org/x/tools/gopls@latest",
        description="Go 官方语言服务器。",
        homepage="https://pkg.go.dev/golang.org/x/tools/gopls",
    ),
    LanguageServerSpec(
        id="rust-analyzer",
        name="rust-analyzer",
        languages=("rust",),
        command=("rust-analyzer",),
        install=("rustup", "component", "add", "rust-analyzer"),
        install_hint="rustup component add rust-analyzer",
        description="Rust 官方语言服务器。",
        homepage="https://rust-analyzer.github.io",
    ),
    LanguageServerSpec(
        id="clangd",
        name="clangd",
        languages=("c", "cpp", "objective-c", "objective-cpp"),
        command=("clangd",),
        install=None,
        install_hint="apt install clangd / brew install llvm",
        description="LLVM 官方 C/C++ 语言服务器，需要 compile_commands.json 才能精确补全。",
        homepage="https://clangd.llvm.org",
    ),
    LanguageServerSpec(
        id="lua",
        name="Lua Language Server",
        languages=("lua",),
        command=("lua-language-server",),
        install=None,
        install_hint="见 github.com/LuaLS/lua-language-server 的 Release 安装包",
        description="Lua 补全、类型注解与诊断。",
        homepage="https://github.com/LuaLS/lua-language-server",
    ),
    LanguageServerSpec(
        id="intelephense",
        name="Intelephense",
        languages=("php",),
        command=("intelephense", "--stdio"),
        install=("npm", "install", "-g", "intelephense"),
        install_hint="npm install -g intelephense",
        description="PHP 高性能语言服务器。",
        homepage="https://intelephense.com",
    ),
    LanguageServerSpec(
        id="solargraph",
        name="Solargraph",
        languages=("ruby",),
        command=("solargraph", "stdio"),
        install=("gem", "install", "solargraph"),
        install_hint="gem install solargraph",
        description="Ruby 补全与文档。",
        homepage="https://solargraph.org",
    ),
    LanguageServerSpec(
        id="jdtls",
        name="Eclipse JDT Language Server",
        languages=("java",),
        command=("jdtls",),
        install=None,
        install_hint="下载 eclipse.jdt.ls 并软链 jdtls 到 PATH（需 JDK 17+）",
        description="Java 官方级语言服务器，支持 Maven/Gradle 项目。",
        homepage="https://github.com/eclipse-jdtls/eclipse.jdt.ls",
    ),
    LanguageServerSpec(
        id="csharp-ls",
        name="C# Language Server",
        languages=("csharp",),
        command=("csharp-ls",),
        install=("dotnet", "tool", "install", "--global", "csharp-ls"),
        install_hint="dotnet tool install --global csharp-ls",
        description="基于 Roslyn 的 C# 语言服务器。",
        homepage="https://github.com/razzmatazz/csharp-language-server",
    ),
    LanguageServerSpec(
        id="taplo",
        name="Taplo",
        languages=("toml",),
        command=("taplo", "lsp", "stdio"),
        install=("cargo", "install", "--locked", "taplo-cli"),
        install_hint="cargo install --locked taplo-cli / npm i -g @taplo/cli",
        description="TOML 补全、格式化与 Schema 校验。",
        homepage="https://taplo.tamasfe.dev",
    ),
    LanguageServerSpec(
        id="marksman",
        name="Marksman",
        languages=("markdown",),
        command=("marksman", "server"),
        install=None,
        install_hint="见 github.com/artempyanykh/marksman 的 Release",
        description="Markdown 标题/链接补全与跳转。",
        homepage="https://github.com/artempyanykh/marksman",
    ),
    LanguageServerSpec(
        id="sqls",
        name="sqls",
        languages=("sql",),
        command=("sqls",),
        install=("go", "install", "github.com/sqls-server/sqls@latest"),
        install_hint="go install github.com/sqls-server/sqls@latest",
        description="SQL 补全与格式化（支持 MySQL/PostgreSQL/SQLite）。",
        homepage="https://github.com/sqls-server/sqls",
    ),
    LanguageServerSpec(
        id="vue",
        name="Vue Language Server",
        languages=("vue",),
        command=("vue-language-server", "--stdio"),
        install=("npm", "install", "-g", "@vue/language-server"),
        install_hint="npm install -g @vue/language-server",
        description="Vue 单文件组件补全与类型检查。",
        homepage="https://github.com/vuejs/language-tools",
    ),
    LanguageServerSpec(
        id="svelte",
        name="Svelte Language Server",
        languages=("svelte",),
        command=("svelteserver", "--stdio"),
        install=("npm", "install", "-g", "svelte-language-server"),
        install_hint="npm install -g svelte-language-server",
        description="Svelte 补全与诊断。",
        homepage="https://github.com/sveltejs/language-tools",
    ),
    LanguageServerSpec(
        id="zls",
        name="ZLS",
        languages=("zig",),
        command=("zls",),
        install=None,
        install_hint="见 github.com/zigtools/zls 的 Release",
        description="Zig 语言服务器。",
        homepage="https://github.com/zigtools/zls",
    ),
    LanguageServerSpec(
        id="prisma",
        name="Prisma Language Server",
        languages=("prisma",),
        command=("prisma-language-server", "--stdio"),
        install=("npm", "install", "-g", "@prisma/language-server"),
        install_hint="npm install -g @prisma/language-server",
        description="Prisma schema 补全与格式化。",
        homepage="https://github.com/prisma/language-tools",
    ),
    LanguageServerSpec(
        id="dart",
        name="Dart Analysis Server",
        languages=("dart",),
        command=("dart", "language-server", "--protocol=lsp"),
        install=None,
        install_hint="安装 Flutter/Dart SDK 后自带",
        description="Dart 官方分析服务器。",
        homepage="https://dart.dev/tools/analysis",
    ),
    LanguageServerSpec(
        id="haskell",
        name="Haskell Language Server",
        languages=("haskell",),
        command=("haskell-language-server-wrapper", "--lsp"),
        install=("ghcup", "install", "hls"),
        install_hint="ghcup install hls",
        description="Haskell 官方语言服务器。",
        homepage="https://haskell-language-server.readthedocs.io",
    ),
    LanguageServerSpec(
        id="elixir-ls",
        name="ElixirLS",
        languages=("elixir",),
        command=("language_server.sh",),
        install=None,
        install_hint="见 github.com/elixir-lsp/elixir-ls 的 Release",
        description="Elixir 补全、跳转与 Dialyzer 诊断。",
        homepage="https://github.com/elixir-lsp/elixir-ls",
    ),
    LanguageServerSpec(
        id="kotlin",
        name="Kotlin Language Server",
        languages=("kotlin",),
        command=("kotlin-language-server",),
        install=None,
        install_hint="见 github.com/Kotlin/kotlin-lsp 的 Release",
        description="Kotlin 补全与跳转。",
        homepage="https://github.com/Kotlin/kotlin-lsp",
    ),
    LanguageServerSpec(
        id="sourcekit-lsp",
        name="SourceKit-LSP",
        languages=("swift",),
        command=("sourcekit-lsp",),
        install=None,
        install_hint="随 Xcode / Swift 工具链安装",
        description="Swift 官方语言服务器。",
        homepage="https://github.com/swiftlang/sourcekit-lsp",
    ),
    LanguageServerSpec(
        id="terraform-ls",
        name="Terraform Language Server",
        languages=("terraform",),
        command=("terraform-ls", "serve"),
        install=None,
        install_hint="brew install hashicorp/tap/terraform-ls",
        description="Terraform HCL 补全与校验。",
        homepage="https://github.com/hashicorp/terraform-ls",
    ),
    LanguageServerSpec(
        id="tinymist",
        name="Tinymist",
        languages=("typst",),
        command=("tinymist",),
        install=("cargo", "install", "--locked", "tinymist"),
        install_hint="cargo install --locked tinymist",
        description="Typst 补全与实时预览。",
        homepage="https://github.com/Myriad-Dreamin/tinymist",
    ),
    LanguageServerSpec(
        id="nil",
        name="nil",
        languages=("nix",),
        command=("nil",),
        install=None,
        install_hint="nix profile install nixpkgs#nil",
        description="Nix 语言服务器。",
        homepage="https://github.com/oxalica/nil",
    ),
    LanguageServerSpec(
        id="solidity",
        name="Solidity Language Server",
        languages=("solidity",),
        command=("nomicfoundation-solidity-language-server", "--stdio"),
        install=("npm", "install", "-g", "@nomicfoundation/solidity-language-server"),
        install_hint="npm install -g @nomicfoundation/solidity-language-server",
        description="Solidity 补全与诊断。",
        homepage="https://github.com/NomicFoundation/hardhat-vscode",
    ),
    LanguageServerSpec(
        id="neocmake",
        name="CMake Language Server",
        languages=("cmake",),
        command=("cmake-language-server",),
        install=("pip", "install", "cmake-language-server"),
        install_hint="pip install cmake-language-server",
        description="CMakeLists.txt 补全。",
        homepage="https://github.com/regen100/cmake-language-server",
    ),
    LanguageServerSpec(
        id="texlab",
        name="TexLab",
        languages=("latex", "bibtex"),
        command=("texlab",),
        install=("cargo", "install", "--locked", "texlab"),
        install_hint="cargo install --locked texlab",
        description="LaTeX 补全、引用跳转与编译诊断。",
        homepage="https://github.com/latex-lsp/texlab",
    ),
    LanguageServerSpec(
        id="graphql",
        name="GraphQL Language Server",
        languages=("graphql",),
        command=("graphql-lsp", "server", "-m", "stream"),
        install=("npm", "install", "-g", "graphql-language-service-cli"),
        install_hint="npm install -g graphql-language-service-cli",
        description="GraphQL Schema 补全与校验。",
        homepage="https://github.com/graphql/graphiql",
    ),
)

_SERVERS_BY_ID: dict[str, LanguageServerSpec] = {spec.id: spec for spec in SERVERS}


def spec_by_id(server_id: str) -> LanguageServerSpec | None:
    """按 id 取服务器描述；未知 id 返回 None。"""

    return _SERVERS_BY_ID.get(server_id)


def servers_for_language(language_id: str) -> tuple[LanguageServerSpec, ...]:
    """所有支持该语言（或该语言的 React 变体）的服务器，按优先级排序。"""

    return tuple(spec for spec in SERVERS if language_id in spec.languages)


def find_server(language_id: str) -> LanguageServerSpec | None:
    """为该语言挑一个服务器：优先已安装的，其次清单里排第一的。"""

    candidates = servers_for_language(language_id)
    for spec in candidates:
        if spec.available():
            return spec
    return candidates[0] if candidates else None


def language_for_path(path: str) -> str | None:
    """按文件名/后缀推断 languageId。"""

    name = Path(path).name
    lowered = name.lower()
    if lowered in FILENAME_LANGUAGES:
        return FILENAME_LANGUAGES[lowered]
    suffix = Path(lowered).suffix
    return EXTENSION_LANGUAGES.get(suffix)


def describe_servers() -> list[dict[str, object]]:
    """给前端用的服务器清单（含是否已安装）。"""

    return [spec.describe() for spec in SERVERS]


def server_available(server_id: str) -> bool:
    spec = _SERVERS_BY_ID.get(server_id)
    return bool(spec and spec.available())
