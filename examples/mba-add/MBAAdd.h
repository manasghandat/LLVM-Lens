//===-- MBAAdd.h - Mixed Boolean-Arithmetic add substitution -------------===//
//
// Substitutes 8-bit integer `add` instructions with the MBA expression
//   a + b == (((a ^ b) + 2 * (a & b)) * 39 + 23) * 151 + 111
// See formula (3) in "Defeating MBA-based Obfuscation" (Eyrolles et al.).
//
//===----------------------------------------------------------------------===//

#ifndef LLVM_LENS_EXAMPLES_MBAADD_MBAADD_H
#define LLVM_LENS_EXAMPLES_MBAADD_MBAADD_H

#include "llvm/IR/PassManager.h"

namespace llvm {

class BasicBlock;

class MBAAdd : public PassInfoMixin<MBAAdd> {
public:
  PreservedAnalyses run(Function &F, FunctionAnalysisManager &AM);
  bool runOnBasicBlock(BasicBlock &BB);
};

} // namespace llvm

#endif // LLVM_LENS_EXAMPLES_MBAADD_MBAADD_H
