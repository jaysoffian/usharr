.PHONY: build
build:
	podman build --platform linux/amd64 \
		--build-arg GIT_COMMIT=$$(git rev-parse --short HEAD) \
		-t usharr:latest .

.PHONY: image
image: build
	podman save -o usharr.tar usharr:latest

.PHONY: serve
serve: config.yaml
	test -f config.yaml || cp usharr/config.yaml.example config.yaml
	USHARR_DB=$(PWD)/usharr.db \
	USHARR_CONFIG=$(PWD)/config.yaml \
	USHARR_DB_RO=1 \
	mise x -- uv run uvicorn usharr.app:app --host 127.0.0.1 --port 8555 --reload

.PHONY: test
test:
	mise x -- uv run prek -a
	mise x -- uv run pytest tests -v

.PHONY: update
update:
	mise x -- prek autoupdate
	mise x -- uv sync --upgrade
