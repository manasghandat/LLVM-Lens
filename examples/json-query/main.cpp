// jsonq: parses a JSON document and evaluates dotted paths against it.
#include "json.h"

namespace {

// A deployment manifest: nested objects, arrays of objects, and both shapes of
// number, in something that still fits on one screen.
constexpr const char *kDocument = R"json({
  "service": { "name": "lens-api", "replicas": 3, "tls": true, "owner": null },
  "hosts": [
    { "addr": "10.0.0.4", "zone": "eu-west-1a", "tags": ["canary", "ssd"] },
    { "addr": "10.0.0.5", "zone": "eu-west-1b", "tags": ["ssd"] },
    { "addr": "10.0.0.6", "zone": "eu-west-1c", "tags": ["burst", "ssd"] }
  ],
  "limits": { "cpu": 1.5, "memory_mb": 2048, "requests_per_sec": 12000 },
  "notes": "drain before \"rolling\" restart\n"
})json";

// What a bare `jsonq` runs: plain lookups, a path into an array, and a miss.
const char *kDefaultQueries[] = {
    "service.name",
    "service.replicas",
    "service.owner",
    "limits.requests_per_sec",
    "hosts[0].addr",
    "hosts[2].tags",
    "service.missing",
};

// Reads *path* into the arena; the view is valid for as long as the arena is.
bool read_file(jsonq::Arena &arena, const char *path, std::string_view &out) {
    std::FILE *file = std::fopen(path, "rb");
    if (file == nullptr)
        return false;

    jsonq::Buffer<char> contents(arena);
    char chunk[4096];
    std::size_t got;
    while ((got = std::fread(chunk, 1, sizeof(chunk), file)) > 0) {
        for (std::size_t i = 0; i < got; i++)
            contents.push(chunk[i]);
    }

    bool ok = std::ferror(file) == 0;
    std::fclose(file);
    if (!ok)
        return false;

    out = std::string_view(contents.data(), contents.size());
    return true;
}

void run(jsonq::Arena &arena, const jsonq::Value *root, const char *query) {
    std::printf("\n%s\n", query);

    jsonq::Path path(arena);
    const char *error = nullptr;
    if (!jsonq::parse_path(query, path, error)) {
        std::printf("  invalid path: %s\n", error);
        return;
    }

    jsonq::Buffer<const jsonq::Value *> found(arena);
    jsonq::select(arena, root, path, found);
    if (found.empty()) {
        std::printf("  no match\n");
        return;
    }

    for (std::size_t i = 0; i < found.size(); i++) {
        std::printf("  ");
        jsonq::print(stdout, *found.data()[i], 1);
        std::printf("\n");
    }
}

} // namespace

int main(int argc, char **argv) {
    jsonq::Arena arena;
    std::string_view text = kDocument;
    int first_query = 1;

    if (argc > 1 && std::string_view(argv[1]) == "-f") {
        if (argc < 3) {
            std::fprintf(stderr, "usage: jsonq [-f document.json] [path ...]\n");
            return 2;
        }
        if (!read_file(arena, argv[2], text)) {
            std::fprintf(stderr, "jsonq: cannot read %s\n", argv[2]);
            return 1;
        }
        first_query = 3;
    }

    jsonq::Parser parser(arena, text);
    const jsonq::Value *root = parser.parse();
    if (root == nullptr) {
        std::fprintf(stderr, "jsonq: %s\n", parser.error());
        return 1;
    }

    std::printf("document: %zu bytes, root %s, %zu bytes allocated\n", text.size(),
                jsonq::kind_name(root->kind), arena.bytes());

    if (first_query >= argc) {
        for (const char *query : kDefaultQueries)
            run(arena, root, query);
    } else {
        for (int i = first_query; i < argc; i++)
            run(arena, root, argv[i]);
    }

    return 0;
}
