## 在 Celery 异步服务中用 MinIO 调用翻译 API 并回存到 MinIO

本示例展示：Celery Worker 从 MinIO 下载 PDF，调用本项目的 HTTP API 翻译，完成后将结果（mono/dual）上传回 MinIO。

### 目录结构

- `examples/celery_minio_worker.py`：Celery 任务脚本

### 前置条件

- 运行中的翻译 API（FastAPI）：参考项目根目录的 `API_README.md`
- 运行中的 MinIO：`MINIO_ENDPOINT` 可达，并准备好输入/输出 bucket
- Celery Broker/Backend：例如 Redis

### 环境变量

必需：
- `CELERY_BROKER_URL`、`CELERY_RESULT_BACKEND`：如 `redis://localhost:6379/0`
- `TRANSLATE_API_BASE`：翻译 API 地址，如 `http://localhost:8000`
- `MINIO_ENDPOINT`、`MINIO_ACCESS_KEY`、`MINIO_SECRET_KEY`、`MINIO_SECURE`
- `MINIO_BUCKET_IN`、`MINIO_BUCKET_OUT`

可选：
- `TRANSLATE_OPTIONS_JSON`：默认 options（JSON 字符串），示例：
  ```json
  {"OPENAI": true, "OPENAI_API_KEY": "你的key", "OPENAI_MODEL": "gpt-4o-mini"}
  ```

### 启动 Celery Worker

```bash
celery -A examples.celery_minio_worker:app worker --loglevel=info
```

### 触发任务

方式一：在 Python 里调用：
```python
from examples.celery_minio_worker import translate_minio_object

result = translate_minio_object.delay(
    object_name="folder/sample.pdf",
    options={
        "OPENAI": True,
        "OPENAI_API_KEY": "你的key",
        "OPENAI_MODEL": "gpt-4o-mini",
        "LANG_IN": "en",
        "LANG_OUT": "zh"
    }
)
print(result.get(timeout=600))
```

方式二：直接运行脚本主函数（需要设置 `TEST_OBJECT`、`TEST_OPTIONS` 环境变量）
```bash
export TEST_OBJECT=folder/sample.pdf
export TEST_OPTIONS='{"OPENAI":true,"OPENAI_API_KEY":"你的key","OPENAI_MODEL":"gpt-4o-mini"}'
python -m examples.celery_minio_worker
```

### 结果说明

- Worker 会将结果上传至 `MINIO_BUCKET_OUT`：
  - `<原对象名去扩展名>.mono.pdf`
  - `<原对象名去扩展名>.dual.pdf`

### 注意事项

- API 的参数规则与优先级与 `API_README.md` 一致：`options` > 环境变量
- 大文件建议适当增大 Celery/HTTP 超时
- MinIO 的 bucket 需提前存在或让脚本自动创建


