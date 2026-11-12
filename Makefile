TEST_TIMEOUT ?= 300s

.PHONY: build build-linux-amd64 test race race-linux-amd64 vet check fuzz smoke vuln dependencies sbom python-test
build:
	python3 scripts/linux_build.py build
build-linux-amd64:
	python3 scripts/linux_build.py build-linux-amd64
test:
	go test -count=1 -timeout=$(TEST_TIMEOUT) ./...
race:
	go test -race -count=1 -timeout=$(TEST_TIMEOUT) ./...
race-linux-amd64:
	python3 scripts/linux_build.py race
vet:
	go vet ./...
check: vet race build python-test
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

python-test:
	python3 -m unittest discover -s scripts -p 'test_*.py'
sbom:
	python3 scripts/generate_sbom.py
