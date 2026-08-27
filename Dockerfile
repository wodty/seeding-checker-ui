# 基础镜像：默认官方源（Docker 需配置代理才能拉取，见 deploy_nas.md）
# 无代理环境换国内源：--build-arg PY_IMAGE=docker.1ms.run/library/python:3.11-slim
ARG PY_IMAGE=python:3.11-slim
FROM ${PY_IMAGE}

# 时区（可选，影响日志时间）
ENV TZ=Asia/Shanghai

WORKDIR /app

COPY requirements.txt .
# 容器内 pip 走清华源（无论代理/直连都可用）
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

COPY . .

# 数据卷：配置 / NAS 目录 / 回收站 均通过 docker-compose 挂载
VOLUME ["/app/config.ini", "/app/trash"]

EXPOSE 8000

CMD ["python", "app.py"]
