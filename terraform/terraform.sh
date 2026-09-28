terraform apply \
  -target=helm_release.metallb \
  -target=helm_release.cert_manager \
  -auto-approve