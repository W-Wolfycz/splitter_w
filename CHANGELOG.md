# 更新日志
## 1.0.0
> 日期：2026-05-18
1. 移除 Simple 分段模式及其相关配置（split_mode、split_chars、clean_before/after_items、enable_smart_split），仅保留正则分段
2. 移除配置迁移逻辑（_migrate_config）
3. 新增符号保护对（protected_pairs）配置：用户可自定义成对符号，内部内容不被切分；数据结构从 dict 改为 list 避免同开字符覆盖
4. 新增保护对分段阈值（protected_split_length）：保护对内容达到指定长度时，在闭合后立即分段；填 0 关闭
5. 重构回复逻辑：enable_reply 作为总开关，enable_smart_reply 作为子选项（插嘴才回复）；关闭时不触碰 Reply 组件，交由框架处理
6. 移除 _remove_reply_components 方法，Reply 组件在分段中始终保留
7. 将内置的 pair_map/quote_chars 硬编码全部暴露到配置文件 default 中
8. 正则引擎从 re 换为 regex，支持 Unicode 属性如 \p{Extended_Pictographic}
