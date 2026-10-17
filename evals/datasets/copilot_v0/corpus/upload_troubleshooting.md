# 单文件上传排查笔记

资料日期：2026-10-16。适用范围：Project Copilot D6，FastAPI 0.138.2、Starlette 1.7.0，公开代码快照3f3159965b94655095acca599333e318dc5287b7。本文是Codex整理的来源草稿，待人工核验；下文分清用户实测与实现契约。

## curl文件表单

上传入口是POST /documents/upload，只接受一个名为file的文件字段。正确的表单参数是 `-F 'file=@路径;type=application/pdf'`。大写-F提交multipart表单并自动选择POST；小写-f表示HTTP错误检查，不会创建文件表单。@必须紧接等号，表示读取本地文件；file=/@路径会变成普通字段内容。

D6用户误写小写-f后实际发送GET，得到405及Allow: POST；在引号内给字段名加入换行后，又因缺少有效file上传字段得到422。修正为file=@路径后进入正常的文件校验。Shell续行反斜杠消除换行，不会自动补空格；--show-error后缺少分隔空格会和下一行URL拼成未知选项，该错误发生在发送请求之前。

## 类型、空文件和额度

实现支持.txt、.md、.pdf，并对后缀及基本MIME做匹配检查；实际内容还要经过UTF-8或PDF解析，不能只凭Content-Type判断文件有效。用户把PDF的上传名称改成sample.exe得到415；0字节TXT得到422“上传文件不能为空”。这些拒绝发生在模型请求前。

默认文件额度是5242880字节，即5MiB。D6用户上传5242881字节的TXT得到413“上传文件超过允许的大小”。框架另外限制整个HTTP请求体，默认额度为文件上限加65536字节，即5308416字节，给multipart封装留空间。这两个检查对象不同；该次用户实测证明了文件额度分支，没有单独手测整个请求体上限。

用户在超限上传时先看见100 Continue，再看见413。100是允许继续发送请求体的临时响应，最终拒绝状态仍是413。

## 临时文件与请求关联

服务端将内容复制到随机的project-copilot-upload-前缀临时文件，原文件名仅作来源元数据。finally负责删除临时副本；用户保存在data/manual-d6的输入样本不会因此删除。D6用户在失败后检查/tmp为0；完成真实上传、问答并停止后端后再次检查，残留临时副本数也为0。

响应头X-Run-ID与完成日志关联；上传成功正文也有同一个run_id。trace_exported=false表示没有导出Phoenix Trace。并发忙碌409、整体body超限及上游异常由自动测试覆盖，本轮用户没有手测这些分支。

## 可追溯来源

- [上传实现](https://github.com/yan-ye-dhy/project-copilot/blob/3f3159965b94655095acca599333e318dc5287b7/src/project_copilot/api/documents.py)
- [整体请求体与run_id](https://github.com/yan-ye-dhy/project-copilot/blob/3f3159965b94655095acca599333e318dc5287b7/src/project_copilot/infrastructure/tracing.py)
- [D6自动与用户验收分别记录](https://github.com/yan-ye-dhy/project-copilot/blob/3f3159965b94655095acca599333e318dc5287b7/docs/reports/DAY_6.md)
- [用户实际命令及纠错记录](https://github.com/yan-ye-dhy/project-copilot/blob/3f3159965b94655095acca599333e318dc5287b7/docs/DEVELOPMENT_LOG.md)
