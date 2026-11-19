"""Live Stream Clips（口播直播 → 自动切片 → 剪辑 → 上传填表）

包结构（对应文档 §6.1 工程结构）：
    app.common          公共层：路径、日志、配置
    app.slicer          「直播切片」服务（录制 / 转写 / LLM 切片 / 打分）
    app.editor          「剪辑工作台」服务（FastAPI + 剪辑管线 + 发布）
"""
