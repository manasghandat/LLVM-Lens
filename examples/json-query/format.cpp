// Writing a value back out as JSON, for the results jsonq prints.
#include "json.h"

#include <cmath>

namespace jsonq {

namespace {

void indent(std::FILE *out, int level) {
    for (int i = 0; i < level; i++)
        std::fputs("  ", out);
}

// Shortest form that reads well; this is for a terminal, not for round-tripping.
void print_number(std::FILE *out, double number) {
    if (number == std::floor(number) && std::fabs(number) < 1e15)
        std::fprintf(out, "%lld", static_cast<long long>(number));
    else
        std::fprintf(out, "%.15g", number);
}

void print_string(std::FILE *out, std::string_view text) {
    std::fputc('"', out);
    for (char c : text) {
        switch (c) {
        case '"':  std::fputs("\\\"", out); break;
        case '\\': std::fputs("\\\\", out); break;
        case '\b': std::fputs("\\b", out);  break;
        case '\f': std::fputs("\\f", out);  break;
        case '\n': std::fputs("\\n", out);  break;
        case '\r': std::fputs("\\r", out);  break;
        case '\t': std::fputs("\\t", out);  break;
        default:
            if (static_cast<unsigned char>(c) < 0x20)
                std::fprintf(out, "\\u%04x", c);
            else
                std::fputc(c, out);
        }
    }
    std::fputc('"', out);
}

} // namespace

void print(std::FILE *out, const Value &value, int level) {
    switch (value.kind) {
    case Kind::Null:
        std::fputs("null", out);
        break;

    case Kind::Bool:
        std::fputs(value.boolean ? "true" : "false", out);
        break;

    case Kind::Number:
        print_number(out, value.number);
        break;

    case Kind::String:
        print_string(out, value.text);
        break;

    case Kind::Array:
        if (value.count == 0) {
            std::fputs("[]", out);
            break;
        }
        std::fputs("[\n", out);
        for (std::size_t i = 0; i < value.count; i++) {
            indent(out, level + 1);
            print(out, *value.items[i], level + 1);
            std::fputs(i + 1 < value.count ? ",\n" : "\n", out);
        }
        indent(out, level);
        std::fputc(']', out);
        break;

    case Kind::Object:
        if (value.count == 0) {
            std::fputs("{}", out);
            break;
        }
        std::fputs("{\n", out);
        for (std::size_t i = 0; i < value.count; i++) {
            const Member &entry = value.members[i];
            indent(out, level + 1);
            print_string(out, entry.key);
            std::fputs(": ", out);
            print(out, *entry.value, level + 1);
            std::fputs(i + 1 < value.count ? ",\n" : "\n", out);
        }
        indent(out, level);
        std::fputc('}', out);
        break;
    }
}

} // namespace jsonq
