# B2: reproducible Spring Boot layered JAR + Temurin 17 JRE
FROM gradle:8.5-jdk17 AS builder
WORKDIR /app
COPY . .
RUN chmod +x ./gradlew
RUN ./gradlew clean bootJar -x test
RUN JAR_FILE=$(find /app/build/libs -maxdepth 1 -type f -name '*.jar' ! -name '*-plain.jar' -print -quit) && java -Djarmode=tools -jar "$JAR_FILE" extract --layers --destination /app/extracted && find /app/extracted -exec touch -h -d '@315532800' {} +

# Runtime image
FROM eclipse-temurin:17-jre
WORKDIR /app
COPY --from=builder /app/extracted/dependencies/ ./
COPY --from=builder /app/extracted/spring-boot-loader/ ./
COPY --from=builder /app/extracted/snapshot-dependencies/ ./
COPY --from=builder /app/extracted/application/ ./

EXPOSE 8080
ENTRYPOINT ["java", "-jar", "/app/spaghetti-0.0.1-SNAPSHOT.jar"]
