# 来源说明

QQ Bot 工具箱由 LXY 发起，vivi 协助开发与维护。八个功能模块共用配置、模型连接、WebUI 和生命周期管理。

- 复读与主动回复、输入屏蔽、观察台、输出替换、随机语音、真实生活及 QQ 空间为本项目维护的模块。
- OpenAI 画图来自整合前的本机画图插件，并持续改造。旧模块元数据记载 `SanHans/astrbot_plugin_openai_image`，保留该记录用于追溯；本项目当前维护者为 LXY 与 vivi。
- 复读模块与空间模块已有的许可证原文分别保留在 `modules/reply/LICENSE` 与 `modules/space/LICENSE`，本包不移除原有声明，也不将整个整合包统一标为 MIT。
- 奶蛙与大肥鱼三视图为用户提供并确认用于本插件的 AI 生成参考图，随包提供。

依赖由 `requirements.txt` 声明并由部署环境安装，不在本包内打包第三方依赖源码。
