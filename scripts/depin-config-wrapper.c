#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <sys/types.h>
#include <sys/stat.h>
#include <pwd.h>
#include <grp.h>
#include <ctype.h>

#define DEPIN_DIR      "/etc/gateway-ui/depin"
#define HONEYGAIN_ENV  "/etc/gateway-ui/depin/honeygain.env"
#define HONEYGAIN_TMP  "/etc/gateway-ui/depin/.honeygain.env.tmp"
#define MASTCHAIN_ENV  "/etc/gateway-ui/depin/mastchain.env"
#define MASTCHAIN_TMP  "/etc/gateway-ui/depin/.mastchain.env.tmp"
#define ANYONE_ETC     "/var/lib/gateway-ui/anyone/etc"
#define ANONRC         "/var/lib/gateway-ui/anyone/etc/anonrc"
#define ANONRC_TMP     "/var/lib/gateway-ui/anyone/etc/.anonrc.tmp"

#define MAX_DEVICE_NAME   64
#define MAX_EMAIL        320
#define MAX_PASSWORD     128
#define MAX_TOKEN        512
#define MAX_NICKNAME      19
#define MAX_CONTACT      255
#define MAX_MYFAMILY    2048
#define MAX_LINE        4096

static void die(const char *msg) {
    fprintf(stderr, "ERROR: %s\n", msg);
    exit(1);
}

static int contains_newline_or_ctrl(const char *s) {
    for (; *s; s++) {
        unsigned char c = (unsigned char)*s;
        if (c < 0x20 || c == 0x7f)
            return 1;
    }
    return 0;
}

static int is_valid_device_name(const char *s) {
    if (!s || *s == '\0')
        return 0;
    size_t len = strlen(s);
    if (len > MAX_DEVICE_NAME)
        return 0;
    for (; *s; s++) {
        if (!isalnum((unsigned char)*s) && *s != '-')
            return 0;
    }
    return 1;
}

static int is_valid_email(const char *s) {
    if (!s || *s == '\0')
        return 0;
    size_t len = strlen(s);
    if (len > MAX_EMAIL)
        return 0;
    const char *at = strchr(s, '@');
    if (!at || at == s || *(at + 1) == '\0')
        return 0;
    if (strchr(at + 1, '.') == NULL)
        return 0;
    if (contains_newline_or_ctrl(s))
        return 0;
    return 1;
}

static int is_valid_password(const char *s) {
    if (!s || *s == '\0')
        return 0;
    size_t len = strlen(s);
    if (len > MAX_PASSWORD)
        return 0;
    return 1;
}

static int is_valid_token(const char *s) {
    /* MastChain dashboard token: non-empty, printable ASCII only (no control
       chars, no newlines — same guard as honeygain's password), and NO spaces:
       it must stay a single argv token for `USERPWD <value>` in the unit's
       ExecStart (a space would split it into two argv entries). */
    if (!s || *s == '\0')
        return 0;
    size_t len = strlen(s);
    if (len > MAX_TOKEN)
        return 0;
    for (; *s; s++) {
        unsigned char c = (unsigned char)*s;
        if (c < 0x21 || c > 0x7e)
            return 0;
    }
    return 1;
}

static int is_valid_nickname(const char *s) {
    if (!s || *s == '\0')
        return 0;
    size_t len = strlen(s);
    if (len < 1 || len > MAX_NICKNAME)
        return 0;
    for (; *s; s++) {
        if (!isalnum((unsigned char)*s))
            return 0;
    }
    return 1;
}

static int is_valid_contact(const char *s) {
    if (!s || *s == '\0')
        return 0;
    size_t len = strlen(s);
    if (len > MAX_CONTACT)
        return 0;
    if (contains_newline_or_ctrl(s))
        return 0;
    return 1;
}

static int is_valid_fingerprint(const char *s) {
    if (!s || *s == '\0')
        return 0;
    size_t len = strlen(s);
    if (len != 40)
        return 0;
    for (; *s; s++) {
        if (!isxdigit((unsigned char)*s))
            return 0;
    }
    return 1;
}

static int is_valid_myfamily(const char *s) {
    if (!s || *s == '\0')
        return 1;
    size_t len = strlen(s);
    if (len > MAX_MYFAMILY)
        return 0;
    char buf[MAX_MYFAMILY + 1];
    strncpy(buf, s, sizeof(buf) - 1);
    buf[sizeof(buf) - 1] = '\0';
    char *token = strtok(buf, ",");
    while (token) {
        while (*token == ' ') token++;
        char *end = token + strlen(token);
        while (end > token && end[-1] == ' ') end--;
        *end = '\0';
        if (!is_valid_fingerprint(token))
            return 0;
        token = strtok(NULL, ",");
    }
    return 1;
}

static void ensure_dir(const char *path, mode_t mode, uid_t uid, gid_t gid) {
    if (mkdir(path, mode) != 0 && errno != EEXIST)
        die("failed to create directory");
    if (chown(path, uid, gid) != 0)
        die("failed to set directory owner");
    if (chmod(path, mode) != 0)
        die("failed to set directory permissions");
}

static void write_file(const char *tmp_path, const char *dst_path,
                       const char *content, mode_t mode,
                       uid_t uid, gid_t gid) {
    int fd = open(tmp_path, O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (fd < 0)
        die("failed to open temp file");
    ssize_t len = (ssize_t)strlen(content);
    if (write(fd, content, len) != len) {
        close(fd);
        unlink(tmp_path);
        die("failed to write temp file");
    }
    if (fsync(fd) != 0) {
        close(fd);
        unlink(tmp_path);
        die("failed to fsync temp file");
    }
    close(fd);
    if (chown(tmp_path, uid, gid) != 0) {
        unlink(tmp_path);
        die("failed to set file owner");
    }
    if (chmod(tmp_path, mode) != 0) {
        unlink(tmp_path);
        die("failed to set file permissions");
    }
    if (rename(tmp_path, dst_path) != 0) {
        unlink(tmp_path);
        die("failed to rename temp file to target");
    }
}

static void cmd_help(void) {
    fprintf(stderr,
        "depin-config-wrapper — write DePIN credential/config files\n"
        "\n"
        "Usage:\n"
        "  depin-config-wrapper honeygain <device_name> <email> <password>\n"
        "  depin-config-wrapper mastchain <email> <token>\n"
        "  depin-config-wrapper anyone     <nickname> <contact> [myfamily]\n");
}

int main(int argc, char *argv[]) {
    struct passwd *pw = getpwnam("gateway-ui");
    if (!pw) {
        fprintf(stderr, "ERROR: gateway-ui user not found on system\n");
        return 1;
    }
    if (getuid() != pw->pw_uid) {
        fprintf(stderr, "ERROR: only gateway-ui user may invoke this wrapper\n");
        return 1;
    }

    if (argc < 2) {
        cmd_help();
        return 1;
    }

    const char *cmd = argv[1];

    if (strcmp(cmd, "--help") == 0 || strcmp(cmd, "-h") == 0) {
        cmd_help();
        return 0;
    }

    setgroups(0, NULL);
    if (setegid(0) != 0)
        die("failed to acquire root gid");
    if (seteuid(0) != 0)
        die("failed to acquire root uid");

    setenv("HOME", "/root", 1);
    setenv("TMPDIR", "/tmp", 1);

    ensure_dir(DEPIN_DIR, 0750, 0, pw->pw_gid);

    if (strcmp(cmd, "honeygain") == 0) {
        if (argc != 5) {
            fprintf(stderr, "ERROR: honeygain requires 3 arguments: <device_name> <email> <password>\n");
            return 1;
        }
        const char *device_name = argv[2];
        const char *email       = argv[3];
        const char *password    = argv[4];

        if (!is_valid_device_name(device_name))
            die("invalid device_name (alphanumeric + hyphen, 1-64 chars)");
        if (!is_valid_email(email))
            die("invalid email");
        if (!is_valid_password(password))
            die("invalid password (non-empty, max 128 chars)");
        if (contains_newline_or_ctrl(password))
            die("password contains control characters");

        char content[MAX_LINE * 4];
        int n = snprintf(content, sizeof(content),
            "DEPIN_DEVICE_NAME=%s\n"
            "DEPIN_HONEYGAIN_EMAIL=%s\n"
            "DEPIN_HONEYGAIN_PASSWORD=%s\n",
            device_name, email, password);
        if (n < 0 || (size_t)n >= sizeof(content))
            die("output too large");

        uid_t owner = 0;
        gid_t group = pw->pw_gid;
        ensure_dir(DEPIN_DIR, 0750, owner, group);
        write_file(HONEYGAIN_TMP, HONEYGAIN_ENV, content, 0640, owner, group);

    } else if (strcmp(cmd, "mastchain") == 0) {
        if (argc != 4) {
            fprintf(stderr, "ERROR: mastchain requires 2 arguments: <email> <token>\n");
            return 1;
        }
        const char *email = argv[2];
        const char *token = argv[3];

        if (!is_valid_email(email))
            die("invalid email");
        if (!is_valid_token(token))
            die("invalid token (printable ASCII, no spaces, max 512 chars)");

        /* Email and token are stored as SEPARATE variables — never combined.
           The USERPWD email:token form is assembled only at command-construction
           in depin-mastchain.service's ExecStart, so the combined secret never
           exists at rest in this file. */
        char content[MAX_LINE * 4];
        int n = snprintf(content, sizeof(content),
            "DEPIN_MASTCHAIN_EMAIL=%s\n"
            "DEPIN_MASTCHAIN_TOKEN=%s\n",
            email, token);
        if (n < 0 || (size_t)n >= sizeof(content))
            die("output too large");

        uid_t owner = 0;
        gid_t group = pw->pw_gid;
        ensure_dir(DEPIN_DIR, 0750, owner, group);
        write_file(MASTCHAIN_TMP, MASTCHAIN_ENV, content, 0640, owner, group);

    } else if (strcmp(cmd, "anyone") == 0) {
        if (argc < 4 || argc > 5) {
            fprintf(stderr, "ERROR: anyone requires 2-3 arguments: <nickname> <contact> [myfamily]\n");
            return 1;
        }
        const char *nickname  = argv[2];
        const char *contact   = argv[3];
        const char *myfamily  = (argc == 5) ? argv[4] : NULL;

        if (!is_valid_nickname(nickname))
            die("invalid nickname (alphanumeric, 1-19 chars per Tor relay spec)");
        if (!is_valid_contact(contact))
            die("invalid contact (non-empty, max 255 chars, no control chars)");
        if (myfamily && !is_valid_myfamily(myfamily))
            die("invalid myfamily (comma-separated 40-char hex fingerprints)");

        char content[MAX_LINE * 20];
        int n = snprintf(content, sizeof(content),
            "User anond\n"
            "DataDirectory /var/lib/anon\n"
            "ControlSocket /run/anon/control\n"
            "ControlSocketsGroupWritable 1\n"
            "CookieAuthentication 1\n"
            "CookieAuthFile /run/anon/control.authcookie\n"
            "CookieAuthFileGroupReadable 1\n"
            "Log notice stdout\n"
            "ORPort 9001\n"
            "SocksPort 0\n"
            "ExitRelay 0\n"
            "Nickname %s\n"
            "ContactInfo %s\n"
            "%s%s%s\n"
            "AgreeToTerms 1\n",
            nickname,
            contact,
            myfamily ? "MyFamily " : "",
            myfamily ? myfamily : "",
            myfamily ? "" : "");
        if (n < 0 || (size_t)n >= sizeof(content))
            die("output too large");

        uid_t owner = 0;
        gid_t group = 0;
        ensure_dir(ANYONE_ETC, 0755, owner, group);
        write_file(ANONRC_TMP, ANONRC, content, 0644, owner, group);

    } else {
        fprintf(stderr, "ERROR: unknown command '%s'\n", cmd);
        cmd_help();
        return 1;
    }

    return 0;
}
