# 이미지·CI 전달 측정

파일 내용과 metadata, registry manifest digest, push 상태, EKS image pull event를 구분해 측정했다. `analyze-dependency-layer-metadata.py`와 `compare-b1-b1r-dependency-tree.py`는 dependency 내용·mtime 원인을 분석한다. `summarize-eks-delivery.py`는 이미지 용량·cold/warm pull 구간을 집계한다.

backend를 다루는 스크립트는 `BACKEND_SOURCE_DIR`로 별도 checkout을 받는다. baseline commit·image repository·태그·builder·cluster 조건은 각 스크립트에 명시돼 있다. 반복 CI와 직접 push의 workflow·측정 도구는 [백엔드 CI](../../backend)에 있다.

`raw/`에는 이미지 전달, hosted baseline, 같은 job의 warm, 독립 warm, direct Buildx의 집계 JSON을 보관했다. 큰 패킷 캡처나 이미지 archive는 포함하지 않는다. 기존 결과를 읽는 것과 새로운 빌드·push·부하 실행은 별도 작업이다.

[수치·표본·측정 정의](../../../EVIDENCE.md#이미지와-ci)
