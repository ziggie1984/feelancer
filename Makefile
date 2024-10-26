PKG := feelancer
UID = $(shell id -u)
GID = $(shell id -g)

black:
	black .

black-check:
	black . --check

isort:
	isort --skip grpc_generated --profile black src/ tests/ itests/

isort-check:
	isort --skip grpc_generated --profile black --check --diff src/ tests/ itests/

ruff:
	ruff check . --fix --exclude grpc_generated

ruff-check:
	ruff check . --exclude grpc_generated

pyright:
	pyright .

format: black
	make isort
	make ruff

check: black-check isort-check ruff-check pyright

clean:
	rm -r $(PKG).egg-info/ || true
	rm -r src/$(PKG).egg-info/ || true
	rm -rf .ruff_cache || true
	rm -rf .pytest_cache || true
	find . -name ".DS_Store" -exec rm -f {} \; || true
	find . -name "__pycache__" -exec rm -rf {} \; || true
	rm -rf dist || true
	rm -rf build || true

test:
	pytest tests --cov-report xml --cov $(PKG)

make compile:
	pip-compile --output-file=requirements.txt base.in
	pip-compile --output-file=addon-requirements.txt base.in addon.in
	pip-compile --output-file=dev-requirements.txt base.in addon.in dev.in

install:
	make clean
	pip install -r requirements.in .

install-dev:
	make clean
	pip install -r dev-requirements.in
	pip install -e .

pyenv_reset:
	pyenv virtualenv-delete -f feelancer-dev
	pyenv virtualenv feelancer-dev

pdf:
	docker run --rm --volume "./docs:/data" --user $(UID):$(GID) pandoc/latex:3.2.1 math.md -o math.pdf

cloc:
	cloc --exclude-dir grpc_generated,.vscode  .
