.PHONY: deploy-prod deploy-staging

deploy-prod:
	fly deploy

deploy-staging:
	fly deploy --config fly.staging.toml

secrets-staging:
	fly secrets set --config fly.staging.toml

secrets-prod:
	fly secrets set
