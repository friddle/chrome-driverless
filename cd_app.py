"""FastAPI 应用实例（各路由/功能模块共享；uvicorn main:app 经 main.py 再导出）。"""
from fastapi import FastAPI

app = FastAPI(title="Chrome Driverless")
