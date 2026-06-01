FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir uv

# Install dependencies first (without the project) for better layer caching
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --frozen --no-install-project

# Copy the package, then install the project itself
COPY app/ app/
RUN uv sync --no-dev --frozen

ENTRYPOINT ["uv", "run", "wacl-sync"]
CMD ["run"]
