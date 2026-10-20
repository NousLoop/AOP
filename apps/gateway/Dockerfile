# syntax=docker/dockerfile:1
FROM golang:1.22-alpine AS build
WORKDIR /src
RUN apk add --no-cache git ca-certificates
# Default: China mirror for local/cloud-CN builds; CI/CD passes proxy.golang.org
ARG GOPROXY=https://goproxy.cn,direct
ENV CGO_ENABLED=0 GOOS=linux GOPROXY=${GOPROXY}
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN go build -o /out/gateway ./cmd

FROM alpine:3.20
RUN apk add --no-cache ca-certificates tzdata wget
WORKDIR /app
COPY --from=build /out/gateway /app/gateway
EXPOSE 8080
ENTRYPOINT ["/app/gateway"]
