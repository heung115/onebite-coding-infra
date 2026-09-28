terraform taint 'helm_release.backend["dev-front"]'
terraform taint 'helm_release.backend["dev-back"]'
terraform taint 'helm_release.nextjs["dev-front"]'
terraform taint 'helm_release.nextjs["dev-back"]'

terraform apply -auto-approve