#!/usr/bin/env bash

# MetalLB 버전 (안정 릴리스)
METALLB_VERSION=v0.14.9

# MetalLB CRD + 컨트롤러·스피커를 한 번에 설치
kubectl apply -f https://raw.githubusercontent.com/metallb/metallb/${METALLB_VERSION}/config/manifests/metallb-native.yaml

kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.15.0/cert-manager.crds.yaml
