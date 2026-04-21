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
	test -f config.yaml || cp config.yaml.example config.yaml
	USHARR_DB=$(PWD)/usharr.db \
	USHARR_CONFIG=$(PWD)/config.yaml \
	USHARR_DB_RO=1 \
	mise x -- uv run uvicorn usharr.app:app --host 127.0.0.1 --port 8555 --reload

.PHONY: update
update:
	mise x -- prek autoupdate
	mise x -- uv sync --upgrade
