// Paths: "servers[1].host" in, the values it selects out.
#include "json.h"

#include <cctype>

namespace jsonq {

namespace {

bool is_name_char(char c) {
    unsigned char byte = static_cast<unsigned char>(c);
    return std::isalnum(byte) != 0 || byte == '_';
}

} // namespace

bool parse_path(std::string_view text, Path &out, const char *&error) {
    out.steps.clear();

    auto reject = [&error](const char *what) {
        error = what;
        return false;
    };

    // An empty path is the document itself; it selects nothing and so the root.
    if (text.empty())
        return true;

    std::size_t at = 0;
    for (;;) {
        std::size_t start = at;
        while (at < text.size() && is_name_char(text[at]))
            at++;
        if (at == start)
            return reject("expected a member name");

        Step step;
        step.kind = Step::Kind::Key;
        step.key = text.substr(start, at - start);
        out.steps.push(step);

        // Any number of subscripts may follow one name: a.b[0][1].
        while (at < text.size() && text[at] == '[') {
            std::size_t digits = ++at;
            std::size_t index = 0;
            while (at < text.size() && text[at] >= '0' && text[at] <= '9') {
                index = index * 10 + static_cast<std::size_t>(text[at] - '0');
                at++;
            }
            if (at == digits || at >= text.size() || text[at] != ']')
                return reject("expected a subscript like [0]");
            at++;

            Step subscript;
            subscript.kind = Step::Kind::Index;
            subscript.index = index;
            out.steps.push(subscript);
        }

        if (at == text.size())
            return true;
        if (text[at] != '.')
            return reject("expected '.' between path segments");
        if (++at == text.size())
            return reject("path ends with '.'");
    }
}

void select(Arena &arena, const Value *root, const Path &path,
            Buffer<const Value *> &out) {
    out.clear();
    if (root == nullptr)
        return;

    Buffer<const Value *> current(arena);
    current.push(root);

    // Each step narrows what the one before it selected.
    for (std::size_t i = 0; i < path.steps.size(); i++) {
        const Step &step = path.steps.data()[i];

        Buffer<const Value *> next(arena);
        for (std::size_t k = 0; k < current.size(); k++) {
            const Value *value = current.data()[k];
            if (step.kind == Step::Kind::Key) {
                if (const Value *found = member(*value, step.key))
                    next.push(found);
            } else if (value->kind == Kind::Array && step.index < value->count) {
                next.push(value->items[step.index]);
            }
        }

        current = next;
        if (current.empty())
            return;
    }

    for (std::size_t i = 0; i < current.size(); i++)
        out.push(current.data()[i]);
}

} // namespace jsonq
