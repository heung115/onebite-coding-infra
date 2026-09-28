# postgresql statefulset 제거(종료)
kubectl delete statefulset postgresql-dev-front 

kubectl delete pvc data-postgresql-dev-front-0

kubectl rollout restart deployment backend-dev-front

terraform taint 'helm_release.postgresql["dev-front"]'

terraform apply
