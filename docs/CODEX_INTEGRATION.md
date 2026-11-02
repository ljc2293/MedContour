# 可选 Codex 任务服务

本目录保留原平台的 Codex worker 源码。核心分割、PDFF 和审核功能通过默认 `compose.yaml` 即可运行。

启用任务发布对话前，在本机准备 `codex-worker/config.toml` 和有效的 `codex-worker/auth.json`。配置模板为 `config.example.toml`；认证文件需使用自己的服务配置。仓库不包含有效凭证，不会自动读取宿主机个人配置。

```bash
docker compose -f compose.yaml -f compose.codex.yaml up -d --build
```

worker 不暴露宿主机端口。源码目录以可写方式挂载到 `/workspace`，任务文件以只读方式挂载到 `/workspace/.medcontour-task-files`。管理员主动触发「让 Codex 实现并测试」后，worker 可以修改源码；普通需求对话和草稿整理阶段限定为分析。

启用前请理解源码写权限及外部模型服务的数据处理方式。需要使用任务服务的部署应独立配置自己的认证、模型和允许提供给服务的数据范围。
