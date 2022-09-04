.PHONY: help venv lint run stage deploy clean

help:
	@echo "Makefile targets:"; \
	echo "  venv   - Create virtualenv with required dependencies"; \
	echo "  lint   - Run linters on sources"; \
	echo "  run    - Run app locally (assumes vimhelp-staging creds exist"; \
	echo "           in the expected filesystem location)"; \
	echo "  stage  - Deploy to staging env (vimhelp-staging.appspot.com)"; \
	echo "  deploy - Deploy to prod env (vimhelp.org)"; \
	echo "  clean  - Delete build artefacts"

venv:
	python3 -m venv --upgrade-deps .venv && \
	.venv/bin/pip install -U wheel && \
	.venv/bin/pip install -U --upgrade-strategy=eager -r requirements.txt

lint:
	flake8
	black --check .

run:
	GOOGLE_APPLICATION_CREDENTIALS=~/private/gcloud-creds/vimhelp-staging-owner.json \
	    GOOGLE_CLOUD_PROJECT=vimhelp-staging VIMHELP_ENV=dev \
	    .venv/bin/gunicorn -k gevent --reload 'vimhelp.webapp:create_app()'

stage: lint
	yes | gcloud app deploy --project=vimhelp-staging

deploy: lint
	gcloud app deploy --project=vimhelp-hrd

clean:
	@[ -n "$$VIRTUAL_ENV" ] && { echo "In virtual env!"; exit 1; } || true
	rm -rf .venv vimhelp/__pycache__
