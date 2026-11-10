.PHONY: build test race vet check fuzz smoke vuln dependencies
build:
	mkdir -p bin
	go build -trimpath -buildvcs=false -o bin/bsc-relay ./cmd/bsc-relay
test:
	go test -count=1 -timeout=120s ./...
race:
	go test -race -count=1 -timeout=120s ./...
vet:
	go vet ./...
check: vet race build
	@test -z "$$(gofmt -l cmd internal)"
	go mod verify
	python3 scripts/check_runtime_dependencies.py
fuzz:
	go test ./internal/relay -run='^$$' -fuzz=FuzzValidateRawTransaction -fuzztime=10s -parallel=2
smoke: build
	python3 scripts/smoke.py bin/bsc-relay
vuln:
	go run golang.org/x/vuln/cmd/govulncheck@v1.9.0 ./...
dependencies:
	python3 scripts/dependencies.py > docs/DEPENDENCIES.json
