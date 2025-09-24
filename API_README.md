## PDFMathTranslate-next HTTP API 使用说明

本文档介绍三种启动方式：本地启动、Docker 单容器、docker-compose。

### 1. 本地启动（开发调试）

前置条件：Python 3.10–3.13、已安装系统依赖（PyMuPDF 运行库），以及对外翻译引擎的 API Key。

1) 安装依赖
```bash
pip install -U uv
uv pip install -r pyproject.toml
uv pip install -e .
```

2) 设置环境变量（示例：SiliconFlow）
```bash
$env:PDF2ZH_SILICONFLOW="true"
$env:PDF2ZH_SILICONFLOW_API_KEY="你的key"
$env:PDF2ZH_LANG_IN="en"
$env:PDF2ZH_LANG_OUT="zh"
$env:PDF2ZH_QPS="10"
$env:PDF2ZH_OUTPUT="./data"
```

3) 启动 API
```bash
uvicorn pdf2zh_next.http_api:app --host 0.0.0.0 --port 8000 --reload
```

### 2. Docker run（单容器）

1) 构建镜像
```bash
docker build -f script/Dockerfile.Api -t pdf2zh-api:latest .
```

2) 运行容器（Windows PowerShell 示例）
```bash
docker run -d --name pdf2zh-api -p 8000:8000 ^
  -e PDF2ZH_OPENAI=true ^ ^
  -e PDF2ZH_OPENAI_API_KEY=你的_openai_key ^ ^
  -e PDF2ZH_OPENAI_BASE_URL=https://api.openai.com/v1 ^ ^
  -e PDF2ZH_OPENAI_MODEL=gpt-4o-mini ^ ^
  -e PDF2ZH_LANG_IN=en -e PDF2ZH_LANG_OUT=zh -e PDF2ZH_QPS=10 ^
  -v "F:\\pdf2zh-data:/data" -v "F:\\pdf2zh-cache:/.cache" pdf2zh-api:latest
```

### 3. docker-compose

1) 复制并根据需要修改 `docker-compose.yml`
```yaml
services:
  api:
    build:
      context: .
      dockerfile: script/Dockerfile.Api
    image: pdf2zh-api:latest
    container_name: pdf2zh-api
    ports:
      - "8000:8000"
    environment:
      # OpenAI 配置（任选其一引擎）
      PDF2ZH_OPENAI: "true"
      PDF2ZH_OPENAI_API_KEY: "${OPENAI_API_KEY}"
      # PDF2ZH_OPENAI_BASE_URL: "https://api.openai.com/v1"   # 可选，兼容端点时使用
      PDF2ZH_OPENAI_MODEL: "gpt-4o-mini"

      # SiliconFlow 示例（如使用则上面 OpenAI 设为 false）
      # PDF2ZH_SILICONFLOW: "true"
      # PDF2ZH_SILICONFLOW_API_KEY: "${SILICONFLOW_API_KEY}"
      # PDF2ZH_SILICONFLOW_MODEL: "Qwen/Qwen2.5-7B-Instruct"

      PDF2ZH_LANG_IN: "en"
      PDF2ZH_LANG_OUT: "zh"
      PDF2ZH_QPS: "10"
      PDF2ZH_REPORT_INTERVAL: "0.2"
    volumes:
      - ./data:/data
      - ./.cache:/.cache
    restart: unless-stopped
```

2) 启动
```bash
docker compose up -d --build
```

3) 更新/停止
```bash
docker compose pull && docker compose up -d
docker compose down
```

### API 调用示例

- 健康检查
```bash
curl http://localhost:8000/healthz
```

- 提交翻译任务
```bash
curl -X POST http://localhost:8000/v1/translate \
  -F "file=@example.pdf" \
  -F "options={\"pdf\":{\"pages\":\"1-\"}}"
```

- 订阅进度（SSE）
```bash
curl http://localhost:8000/v1/translate/<task_id>/events
```

- 查询状态
```bash
curl http://localhost:8000/v1/translate/<task_id>/status
```

- 下载结果
```bash
curl -L "http://localhost:8000/v1/translate/<task_id>/result?type=mono" --output mono.pdf
curl -L "http://localhost:8000/v1/translate/<task_id>/result?type=dual" --output dual.pdf
```

- 取消任务
```bash
curl -X DELETE http://localhost:8000/v1/translate/<task_id>
```

### 参数传递规则（options vs 环境变量）

- 通过 options（/v1/translate 的表单字段）
  - 不需要前缀，键名使用全大写。
  - 示例（OpenAI）:
    - `"OPENAI": true`
    - `"OPENAI_API_KEY": "你的key"`
    - `"OPENAI_BASE_URL": "https://api.openai.com/v1"`
    - `"OPENAI_MODEL": "gpt-4o-mini"`

- 通过环境变量（Docker/.env/系统环境）
  - 必须加前缀 `PDF2ZH_`，后面是大写字段名（下划线分隔）。
  - 示例（OpenAI）:
    - `PDF2ZH_OPENAI=true`
    - `PDF2ZH_OPENAI_API_KEY=你的key`
    - `PDF2ZH_OPENAI_BASE_URL=https://api.openai.com/v1`
    - `PDF2ZH_OPENAI_MODEL=gpt-4o-mini`

- 优先级
  - `options` > 环境变量（同名配置以 `options` 为准）。

### 说明与建议

- 环境变量前缀 `PDF2ZH_`，与配置字段一一映射；典型组合：
  - 引擎选择与密钥：`PDF2ZH_SILICONFLOW=true`、`PDF2ZH_SILICONFLOW_API_KEY=...`
  - 语言与速率：`PDF2ZH_LANG_IN`、`PDF2ZH_LANG_OUT`、`PDF2ZH_QPS`
  - 输出目录：`PDF2ZH_OUTPUT`（容器默认 `/data`）
- 结果文件默认保存到 `PDF2ZH_OUTPUT/<task_id>/` 目录。
- Windows 路径挂载请使用完整盘符路径；Linux/macOS 请使用绝对路径。
- 密钥请通过 `.env` 或 Secret 管理，避免写入镜像层与日志。


