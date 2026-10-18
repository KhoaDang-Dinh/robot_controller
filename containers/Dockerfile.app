# One application image for every experiment. The experiment file chooses the
# adapter and policy at run time, so changing hyperparameters needs no rebuild.
#
#   docker build -f containers/Dockerfile.app -t ghcr.io/khoadang-dinh/robot-sim-worker:dev .
#
# BASE_IMAGE is the slow-moving layer (Python today; CUDA / Isaac Sim later, pinned
# by digest). Code changes only rebuild the thin layers below it.
ARG BASE_IMAGE=python:3.12-slim

FROM ${BASE_IMAGE} AS build
ARG UV_VERSION=0.12.21
ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
RUN pip install "uv==${UV_VERSION}"
WORKDIR /src
# Dependencies first (cached until uv.lock changes), then the project itself.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM ${BASE_IMAGE}
LABEL org.opencontainers.image.source="https://github.com/KhoaDang-Dinh/robot_controller" \
      org.opencontainers.image.title="robot-sim-worker" \
      org.opencontainers.image.description="Robot simulation runner: stable recovery core with pluggable simulators and policies" \
      org.opencontainers.image.licenses="NOASSERTION"
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH \
    OTEL_SERVICE_NAME=robot-sim-worker \
    IMAGE_NAME=ghcr.io/khoadang-dinh/robot-sim-worker \
    EXPERIMENT_FILE=/app/experiments/smoke-resume.yaml
RUN groupadd --gid 10001 workload \
    && useradd --create-home --uid 10001 --gid 10001 workload
COPY --from=build /opt/venv /opt/venv
COPY experiments /app/experiments

USER 10001:10001
HEALTHCHECK NONE
ENTRYPOINT ["robot-run"]
CMD ["run"]
