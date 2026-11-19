"""「剪辑工作台」服务（editor）后端包。

模块划分（对应文档 §6.1）：
    main.py         FastAPI 应用工厂 + 启动入口
    __main__.py     ``python -m app.editor.backend``
    database.py     SQLite + 版本化 migration
    security.py     session token 鉴权
    api/            路由
    domain/         领域模型
    services/       业务服务（剪辑管线、录制、发布等）
"""
