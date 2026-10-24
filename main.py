"""Agent 会诊室 —— HTTP 服务入口。

只做三件事：
1. 暴露只读分析 API（全部基于已采集的轨迹数据集，运行时不需要任何外部模型密钥）
2. 托管前端构建产物
3. 提供自检接口，方便部署后确认环境是否正常

启动方式遵循平台约定：监听 $PORT，绑定 0.0.0.0。
"""

from __future__ import annotations

import os

from server.app import create_app

app = create_app()


def main() -> None:
    import uvicorn

    port = int(os.environ.get("PORT", "3000"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
