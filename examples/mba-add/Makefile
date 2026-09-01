# Build the MBAAdd pass plugin as a shared object.
# Pinned to LLVM 22 to match LLVM-Lens's toolchain.
LLVM_CONFIG := $(shell command -v llvm-config-22 || command -v llvm-config)
CXX         := $(shell $(LLVM_CONFIG) --bindir)/clang++
CXXFLAGS    := -fPIC -shared $(shell $(LLVM_CONFIG) --cxxflags)
TARGET      := libMBAAdd.so

all: $(TARGET)

$(TARGET): MBAAdd.cpp MBAAdd.h
	$(CXX) $(CXXFLAGS) MBAAdd.cpp -o $@

clean:
	rm -f $(TARGET)

.PHONY: all clean
