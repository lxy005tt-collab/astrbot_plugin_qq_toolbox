# 第三方来源

QQ 空间 HTTP 协议封装中的发布表单、图片上传字段和 richval 结构，改写自：

- https://github.com/Zhalslar/astrbot_plugin_qzone
- 参考提交：36d4a18e863234d69c94976cdca9143a5e003b42
- 文件：core/qzone/api.py、parser.py、session.py、model.py
- 许可证：GPL-3.0，全文见本目录 LICENSE。

留言板 URL、参数和响应字段参考：

- https://github.com/guohuiyuan/qzone-go/blob/main/api_board.go
- 本项目仅按协议字段独立实现 Python 读取逻辑，未复制 Go 实现。

本插件的 AstrBot 命令、工具、权限、发布请求记录及图片处理由 vivi 为本地需求编写。
本插件按 GPL-3.0 分发；不是对所有接口代码均为原创的声明。
