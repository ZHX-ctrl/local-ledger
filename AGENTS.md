# 本地账本操作约定

本项目真实数据全部在 private/，禁止通过 C2C、网页端 ChatGPT、子代理或其他外部服务读取/发送。开发审查仅用源码与虚构数据。不要删除或上传 private/、照片、备份、导出。不得把金融数据写入 REQUIREMENTS.md、CONTRACT.md、测试、Git、日志或公共文档。普通对话里用户主动提供的信息用于已授权记账；原始照片不自动上传任何识别服务。

## 对话记账
1. 日期必须含明确年份（从当前对话确凿上下文可解析），币种必须明确。缺失/歧义先补问，不默认今天或新币。保留用户自填名称。分类明确可推断，不确定先询问；明确指定优先。
2. 只通过运行中网站的本机接口写入。使用 `python -m ledger_app.cli state --month YYYY-MM` 查看分类ID和钱包初始化状态；不要直接 SQL 编辑。
3. payload JSON 写入 private/work/，用 `python -m ledger_app.cli command --action entry.save --request-id <唯一请求号> --file <私有文件>`。调用不打印 session/CSRF 凭据。同一逻辑操作失败重试保持请求号，不重建请求号绕过幂等。
4. 相同日期/币种/金额/名称的疑似重复提示必须向用户确认后，才增加 confirm_duplicate:true。缺少钱包基准时让用户输入真实余额及截至日期，不编造初始值。截止前账目只更新历史统计。
5. 只有 ok:true 且返回记录后才能说“已记录”；回执给编号、日期、名称、类别、原币金额、折算金额或待更新。失败说明原因，不假装写入。修改/删除用对应正式 action，保持可审计。
6. 用户未明确指定的示例不是账目，不录入。照片使用本地编码经 photo.save 后关联，不以任意路径替代 photo_id。

## 开发验证
Python 标准库 unittest，测试用临时目录。UI 测试必须 --data-dir private/work/ui-test 等隔离目录，不在真实账本填假数据。服务只监听 127.0.0.1。禁止暴露本机账本 HTTP 端口给 Cloudflare。参考汇率唯一出口是 Frankfurter ECB，发送币种与日期，不发送任何金额/名称/照片。未经用户明确请求不修改记忆库。
