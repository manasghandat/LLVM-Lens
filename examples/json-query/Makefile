# Build jsonq, and the one linked IR module the report reads.
# Pinned to LLVM 22 to match LLVM-Lens's toolchain.
LLVM_CONFIG := $(shell command -v llvm-config-22 || command -v llvm-config)
BINDIR      := $(shell $(LLVM_CONFIG) --bindir)
CLANG       := $(BINDIR)/clang++
LLVM_LINK   := $(BINDIR)/llvm-link
LLVM_DIS    := $(BINDIR)/llvm-dis

SOURCES := $(wildcard *.cpp)
HEADERS := json.h
UNITS   := $(patsubst %.cpp,build/%.ll,$(SOURCES))
MODULE  := build/json-query.ll
BINARY  := build/jsonq

# -O0 with -disable-O0-optnone and -g is what LLVM-Lens passes to clang itself,
# so the module opt reads here is shaped like one it would have built.
IRFLAGS := -S -emit-llvm -O0 -Xclang -disable-O0-optnone -g -std=c++17

all: $(BINARY) $(MODULE)

$(BINARY): $(SOURCES) $(HEADERS) | build
	$(CLANG) -O2 -std=c++17 $(SOURCES) -o $@

# $(CURDIR) so the paths in the debug info resolve from anywhere: the report
# reads them back to put the source beside the IR.
build/%.ll: %.cpp $(HEADERS) | build
	$(CLANG) $(IRFLAGS) $(CURDIR)/$< -o $@

# llvm-link writes bitcode whatever the output is called, so the disassembly is
# its own step; the report reads the text.
$(MODULE): $(UNITS)
	$(LLVM_LINK) $(UNITS) -o $(MODULE:.ll=.bc)
	$(LLVM_DIS) $(MODULE:.ll=.bc) -o $@

build:
	@mkdir -p build

run: $(BINARY)
	$(BINARY)

# The whole default<O2> pipeline: minutes, and a few hundred MB of report.
# Scope it down with --passes if you only want to read part of it.
report: $(MODULE)
	llvm-lens $(MODULE) --open

clean:
	rm -rf build

.PHONY: all run report clean
