"""
Dashboard 启动脚本

用法：
    python -m dashboard.server --project-root /path/to/novel-project
    python -m dashboard.server                   # 使用统一项目定位
"""

import argparse
import sys
import webbrowser
from pathlib import Path


def _shared_resolver():
    scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
    scripts_entry = str(scripts_dir)
    if scripts_entry not in sys.path:
        sys.path.insert(0, scripts_entry)
    from project_locator import resolve_project_root

    return resolve_project_root


def _resolve_project_root(cli_root: str | None) -> Path:
    """Use the same strict resolver as every runtime command."""
    try:
        return _shared_resolver()(cli_root)
    except FileNotFoundError as exc:
        print(f"ERROR: 无法定位 PROJECT_ROOT（需要包含 .webnovel/state.json 的目录）: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


def main():
    parser = argparse.ArgumentParser(description="Webnovel Dashboard Server")
    parser.add_argument("--project-root", type=str, default=None, help="小说项目根目录")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=8765, help="监听端口")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    project_root = _resolve_project_root(args.project_root)
    print(f"项目路径: {project_root}")

    # 延迟导入，以便先处理路径
    import uvicorn
    from .app import create_app

    app = create_app(project_root)

    url = f"http://{args.host}:{args.port}"
    print(f"Dashboard 启动: {url}")
    print(f"API 文档: {url}/docs")

    if not args.no_browser:
        webbrowser.open(url)

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
