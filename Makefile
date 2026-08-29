BINARY := llvm-lens
PKG    := ./cmd/llvm-lens

.PHONY: all build run test vet fmt tidy clean

all: build

build:
	go build -o bin/$(BINARY) $(PKG)

## run: start the server on :8080 (override with ADDR=:9000)
run:
	go run $(PKG) -addr $(or $(ADDR),:8080)

test:
	go test ./...

vet:
	go vet ./...

fmt:
	gofmt -l -w .

tidy:
	go mod tidy

clean:
	rm -rf bin/
