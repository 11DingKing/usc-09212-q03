"""项目服务入口。

独立部署示例：
    python3 -m service.main --db ./data/compliance.db --seed --worker --port 8000
"""
import argparse
import os
from http.server import ThreadingHTTPServer

from .api import ApiHandler, build_app


def run(host="127.0.0.1", port=8000, db_path=":memory:", seed=False,
        worker=True):
    """启动合规判定服务。"""
    app = build_app(db_path, with_worker=worker, seed=seed)
    ApiHandler.app = app
    server = ThreadingHTTPServer((host, port), ApiHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop_worker()
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description="跨境算法合规判定服务")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--db", default=os.environ.get(
        "COMPLIANCE_DB", ":memory:"),
        help="SQLite 路径；默认 :memory:（重启不保留）")
    parser.add_argument("--seed", action="store_true",
                        help="首次启动载入示例规则包")
    parser.add_argument("--worker", action="store_true",
                        help="启用后台评估工作线程")
    args = parser.parse_args()
    run(args.host, args.port, args.db, args.seed, args.worker)


if __name__ == "__main__":
    main()
