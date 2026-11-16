# Reproducible run: no runtime dependencies, pytest only for the test target.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . pytest==8.3.3
COPY tests ./tests
COPY bench ./bench
COPY examples ./examples
# Customer files are mounted at /data; nothing leaves the container.
ENTRYPOINT ["omap"]
CMD ["--help"]
