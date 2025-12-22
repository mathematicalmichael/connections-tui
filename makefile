bump:
	uv version --bump patch
	git commit -a --amend

pub:
	uv build
	uv publish

lint:
	uvx black src
	uvx isort --profile=black src
	uvx ruff check . --fix
