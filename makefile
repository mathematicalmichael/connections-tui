bump:
	uv version --bump patch
	git commit -a --amend

pub:
	uv build
	uv publish
