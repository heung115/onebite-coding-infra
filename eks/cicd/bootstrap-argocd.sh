#!/usr/bin/env bash
set -euo pipefail

region="${AWS_REGION:-ap-northeast-2}"
cluster="${EKS_CLUSTER_NAME:-onebite-cicd}"
kubeconfig_tmp="$(mktemp "${TMPDIR:-/tmp}/onebite-cicd-kubeconfig.XXXXXX")"
chmod 600 "${kubeconfig_tmp}"
trap 'unlink "${kubeconfig_tmp}"' EXIT

aws eks update-kubeconfig \
  --name "${cluster}" \
  --region "${region}" \
  --kubeconfig "${kubeconfig_tmp}" >/dev/null

kubectl --kubeconfig "${kubeconfig_tmp}" get namespace argocd >/dev/null

create_repo_secret_manifest() {
  if [[ -n "${ARGOCD_REPO_KEY_FILE:-}" ]]; then
    test -r "${ARGOCD_REPO_KEY_FILE}"
    kubectl --kubeconfig "${kubeconfig_tmp}" create secret generic repo-tmp-onebite-gitops \
      --namespace argocd \
      --from-literal=type=git \
      --from-literal=url=git@github.com:heung115/tmp-onebite-gitops.git \
      --from-file="sshPrivateKey=${ARGOCD_REPO_KEY_FILE}" \
      --dry-run=client \
      --output yaml
  else
    aws secretsmanager get-secret-value \
      --secret-id onebite-argocd-repo \
      --region "${region}" \
      --query SecretString \
      --output text |
      jq -er '.sshPrivateKey' |
      kubectl --kubeconfig "${kubeconfig_tmp}" create secret generic repo-tmp-onebite-gitops \
        --namespace argocd \
        --from-literal=type=git \
        --from-literal=url=git@github.com:heung115/tmp-onebite-gitops.git \
        --from-file=sshPrivateKey=/dev/stdin \
        --dry-run=client \
        --output yaml
  fi
}

create_repo_secret_manifest |
  kubectl --kubeconfig "${kubeconfig_tmp}" apply -f -

kubectl --kubeconfig "${kubeconfig_tmp}" label secret repo-tmp-onebite-gitops \
  --namespace argocd \
  argocd.argoproj.io/secret-type=repository \
  --overwrite >/dev/null

kubectl --kubeconfig "${kubeconfig_tmp}" apply -f "$(dirname "${BASH_SOURCE[0]}")/root-app.yaml"
