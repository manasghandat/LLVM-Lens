// Reading a parsed document: the accessors every other unit calls.
#include "json.h"

namespace jsonq {

const char *kind_name(Kind kind) {
    switch (kind) {
    case Kind::Null:   return "null";
    case Kind::Bool:   return "bool";
    case Kind::Number: return "number";
    case Kind::String: return "string";
    case Kind::Array:  return "array";
    case Kind::Object: return "object";
    }
    return "unknown";
}

const Value *member(const Value &object, std::string_view key) {
    if (object.kind != Kind::Object || object.count == 0)
        return nullptr;

    // The parser sorted the members, so this is a search and not a scan.
    std::size_t low = 0;
    std::size_t high = object.count;
    while (low < high) {
        std::size_t middle = low + (high - low) / 2;
        const Member &entry = object.members[middle];
        if (entry.key < key) {
            low = middle + 1;
        } else if (key < entry.key) {
            high = middle;
        } else {
            return entry.value;
        }
    }
    return nullptr;
}

} // namespace jsonq
