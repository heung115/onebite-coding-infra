# EKS 구성과 autoscaler 검증

`aws/`는 VPC·EKS·Pod Identity·ALB Controller·EBS CSI·Secrets Manager·Argo CD와 앱을 관리하는 Terraform root다. `cicd/`는 CI와 GitOps 연결을 확인한 별도 root다. `measure/terraform/`는 CA/Karpenter 비교·Spot FIS를 위한 root다. 각 root는 별도 state를 사용한다.

`charts/`, `values/`, `values-eks/`는 root가 참조하는 차트와 설정이다. Redis·PostgreSQL은 사용한 chart archive도 보관했다. 실제 계정·API CIDR는 terraform.tfvars로 지정하고 공개 예시의 계정과 CIDR는 교체한다. `.tfvars`·state·kubeconfig·plan은 Git에서 제외한다.

`measure/manifests/`에서 같은 AZ·AMI·instance·On-Demand의 scale-out, Fixed/Flexible requests, Spot HTTP probe 조건을 확인할 수 있다. `measure/scripts/`는 수집·조건 검증·집계를 담당하며 `measure/results/`에는 최종 작은 분석 JSON과 paired 개별 결과를 보관했다. `measure/scripts/tf.sh`는 리전·계정·mutation 입력을 확인하는 wrapper다.

`scripts/put-secrets.sh`는 ONEBITE_SECRET_INPUT의 외부 JSON에서 secret 이름→값을 읽고 AWS CLI stdin으로 전달한다. 실제 값을 파일·명령 출력·저장소에 남기지 않으며, GitOps에는 Secret 참조를 둔다. 값 입력 파일은 저장소 밖에서 관리한다.

[확장·요율 결과](../EVIDENCE.md#eks-확장과-용량) · [Spot HTTP 결과](../EVIDENCE.md#spot-종료-중-http-요청)
