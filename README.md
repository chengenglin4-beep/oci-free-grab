# OCI 免费实例自动重试

[查看抢机运行记录](https://github.com/chengenglin4-beep/oci-free-grab/actions/workflows/oci-grab.yml)

每 5 分钟在 GitHub 公共仓库的免费标准运行器启动一次；运行中遇到容量不足会等待 130–150 秒后继续尝试。遇到 429 限流会暂停至少 300 秒。电脑可以关机。GitHub 排队可能导致延迟，无法保证抢到时间。

固定目标：新加坡 AD-1，sg-node-01，VM.Standard.A1.Flex，2 OCPU / 12 GB，Ubuntu 24.04 Minimal aarch64，默认启动卷，公网 IPv4。只使用已有子网，不创建其他网络或额外块存储，不升级、不删除云资源。

## 查看是否抢到

打开最新运行的 Retry free A1 instance 日志：
- Out of host capacity：容量不足，等待后重试。
- 429 rate limited：触发限流，暂停至少 300 秒。
- Launch accepted：创建请求已接受，请到 OCI 实例页面查看状态和公网 IP。
- Instance was already created：已提交成功，不再创建。
- Safety stop / Safety lock is not idle：异常停止，需核对后处理。

绿色勾表示本轮执行正常，**不等于抢到实例**。

state.json：idle 表示等待下一轮；pending 表示上次提交结果待核对，阻止重复创建；created 表示请求已接受；authentication_failed 表示认证失败，停止创建。不要直接清空异常状态，也不要同时运行本机抢机脚本。

API 密钥和账户配置存放在 Actions 加密 Secrets，不放进代码和日志。SSH 私钥留在本机。

仅启用手动和定时触发；没有外部拉取请求触发器。GitHub 公共仓库长时间没有活动可能停用定时任务，请在 Actions 页面检查状态。抢到实例后可停用该工作流。
