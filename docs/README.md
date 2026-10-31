# python-github-ci-tracker

A self-hosted GitHub Actions tracker. This directory holds the documentation; the
root README states what the project does and why.

## Documentation / 文档

Please select your language / 请选择语言：

- [English Documentation](en_US/README.md)
- [简体中文文档](zh_CN/README.md)

## Key Features

- **Three protocol surfaces, one flow**: MCP, REST and CLI over the same controller.
- **Run → job → step → log**, one object per layer, one tool per layer.
- **Step logs are captured, not read**: GitHub serves a run split into per-step
  files for about an hour after it finishes; after that the same endpoint returns
  one merged document per job.
- **Log windows address, they do not interpret**: tail, an explicit range, or a
  caller-supplied pattern.
- **PostgreSQL only**: monthly partitions on the log tables, so dropped data is
  one `DROP TABLE` instead of a locking vacuum.
