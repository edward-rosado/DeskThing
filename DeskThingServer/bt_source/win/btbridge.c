/* DeskThing Bluetooth bridge (Windows side).
 *
 * Connects to the Car Thing's RFCOMM channel 3 and demuxes tunneled TCP
 * streams onto localhost:8891 (the DeskThing server), mirroring the macOS
 * helper's control API on 127.0.0.1:8899 (see bt_source/README.md).
 *
 * Build (done by bt_source/build-btbridge.js when a compiler is present):
 *   cl /O2 btbridge.c /link ws2_32.lib Bthprops.lib
 *   — or —
 *   clang -O2 btbridge.c -o btbridge.exe -lws2_32 -lBthprops
 *
 * Frame: type(1) streamID(4 BE) len(2 BE) payload. 1=OPEN 2=DATA 3=CLOSE.
 */
#include <winsock2.h>
#include <ws2bth.h>
#include <windows.h>
#include <bluetoothapis.h>
#include <stdio.h>
#include <time.h>
#include <stdlib.h>
#include <string.h>

#pragma comment(lib, "ws2_32.lib")
#pragma comment(lib, "Bthprops.lib")

#define RFCOMM_CHANNEL 3
#define SERVER_PORT 8891
#define CONTROL_PORT 8899
#define CHUNK 660
#define MAX_STREAMS 64

/* --- protocol v2 --------------------------------------------------------
 * v1 was one-directional: only the device opened streams, always to the
 * DeskThing server. v2 lets either side open, and an OPEN carries a target.
 * The stream-ID space is split by its high bit so both ends can allocate
 * without coordinating: the device keeps the low half, we take the high half. */
#define PROTOCOL_VERSION 2
#define NS_MASK 0x80000000u
#define CAP_INBOUND 0x0001
#define CAP_TARGETED 0x0002
#define OUR_CAPS (CAP_INBOUND | CAP_TARGETED)
#define KIND_SERVICE 0x01
#define ACK_OK 0
#define MAX_FORWARDS 4

/* Services on the DEVICE this computer may open. The device enforces the same
 * list independently; this copy lets us refuse early. */
static const char *DEVICE_SERVICES[] = { "cdp", "pairing", NULL };

static CRITICAL_SECTION g_lock;

/* ---- shared state ------------------------------------------------- */

static char g_preference[16] = "bluetooth";
static volatile int g_link_up = 0;
static char g_device_address[32] = "";      /* AA:BB:CC:DD:EE:FF */
static char g_pairing_stage[16] = "idle";
static char g_pairing_code[8] = "";
static char g_pairing_error[128] = "";
static char g_found[2048] = "";             /* pre-rendered JSON array body */

static char g_state_path[MAX_PATH];

static void logline(const char *fmt, ...) {
  va_list ap;
  SYSTEMTIME st;
  GetLocalTime(&st);
  printf("[%02d:%02d:%02d] ", st.wHour, st.wMinute, st.wSecond);
  va_start(ap, fmt);
  vprintf(fmt, ap);
  va_end(ap);
  printf("\n");
  fflush(stdout);
}

static void state_path_init(void) {
  const char *appdata = getenv("APPDATA");
  snprintf(g_state_path, sizeof(g_state_path), "%s\\deskthing\\bt-transport.json",
           appdata ? appdata : ".");
}

static void state_save(void) {
  char dir[MAX_PATH];
  const char *appdata = getenv("APPDATA");
  FILE *f;
  snprintf(dir, sizeof(dir), "%s\\deskthing", appdata ? appdata : ".");
  CreateDirectoryA(dir, NULL);
  f = fopen(g_state_path, "w");
  if (!f) return;
  if (g_device_address[0])
    fprintf(f, "{\n \"preference\": \"%s\",\n \"deviceAddress\": \"%s\"\n}\n",
            g_preference, g_device_address);
  else
    fprintf(f, "{\n \"preference\": \"%s\"\n}\n", g_preference);
  fclose(f);
}

/* Minimal parse: find "key":"value" in a small JSON blob. */
static int json_str(const char *json, const char *key, char *out, size_t cap) {
  char pat[64];
  const char *p, *q;
  snprintf(pat, sizeof(pat), "\"%s\"", key);
  p = strstr(json, pat);
  if (!p) return 0;
  p = strchr(p + strlen(pat), '"');
  if (!p) return 0;
  q = strchr(p + 1, '"');
  if (!q || (size_t)(q - p - 1) >= cap) return 0;
  memcpy(out, p + 1, q - p - 1);
  out[q - p - 1] = 0;
  return 1;
}

static void state_load(void) {
  char buf[512];
  FILE *f = fopen(g_state_path, "r");
  size_t n;
  if (!f) return;
  n = fread(buf, 1, sizeof(buf) - 1, f);
  buf[n] = 0;
  fclose(f);
  json_str(buf, "preference", g_preference, sizeof(g_preference));
  json_str(buf, "deviceAddress", g_device_address, sizeof(g_device_address));
}

/* ---- adb arbitration ---------------------------------------------- */

static int run_adb(const char *args) {
  char exe[MAX_PATH], adb_exe[MAX_PATH], cmd[MAX_PATH * 2];
  STARTUPINFOA si;
  PROCESS_INFORMATION pi;
  DWORD code = (DWORD)-1;
  GetModuleFileNameA(NULL, exe, sizeof(exe));
  {
    char *slash = strrchr(exe, '\\');
    if (slash) *slash = 0;
  }
  snprintf(adb_exe, sizeof(adb_exe), "%s\\adb.exe", exe);
  if (GetFileAttributesA(adb_exe) != INVALID_FILE_ATTRIBUTES)
    snprintf(cmd, sizeof(cmd), "\"%s\" %s", adb_exe, args);
  else
    snprintf(cmd, sizeof(cmd), "adb %s", args);
  memset(&si, 0, sizeof(si));
  si.cb = sizeof(si);
  si.dwFlags = STARTF_USESHOWWINDOW;
  si.wShowWindow = SW_HIDE;
  if (!CreateProcessA(NULL, cmd, NULL, NULL, FALSE, CREATE_NO_WINDOW, NULL, NULL, &si, &pi))
    return -1;
  WaitForSingleObject(pi.hProcess, 15000);
  GetExitCodeProcess(pi.hProcess, &code);
  CloseHandle(pi.hProcess);
  CloseHandle(pi.hThread);
  return (int)code;
}

static void prefer_bluetooth(void) {
  int rc = run_adb("reverse --remove tcp:8891");
  logline("transport: bluetooth active (USB reverse removed, rc=%d)", rc);
}

static void fall_back_to_usb(void) {
  int rc = run_adb("reverse tcp:8891 tcp:8891");
  logline(rc == 0 ? "transport: USB active (adb reverse restored)"
                  : "transport: USB unavailable (rc=%d) — device likely unplugged", rc);
}

static const char *active_transport(void) {
  if (g_link_up) return "bluetooth";
  return run_adb("get-state") == 0 ? "usb" : "none";
}

/* ---- address helpers ----------------------------------------------- */

static int parse_addr(const char *s, BTH_ADDR *out) {
  unsigned b[6];
  char norm[32];
  size_t i, j = 0;
  for (i = 0; s[i] && j < sizeof(norm) - 1; i++)
    norm[j++] = (s[i] == '-') ? ':' : s[i];
  norm[j] = 0;
  if (sscanf(norm, "%x:%x:%x:%x:%x:%x", &b[0], &b[1], &b[2], &b[3], &b[4], &b[5]) != 6)
    return 0;
  *out = 0;
  for (i = 0; i < 6; i++) *out = (*out << 8) | (BTH_ADDR)(b[i] & 0xff);
  return 1;
}

static void format_addr(BTH_ADDR a, char *out, size_t cap) {
  snprintf(out, cap, "%02X:%02X:%02X:%02X:%02X:%02X",
           (unsigned)((a >> 40) & 0xff), (unsigned)((a >> 32) & 0xff),
           (unsigned)((a >> 24) & 0xff), (unsigned)((a >> 16) & 0xff),
           (unsigned)((a >> 8) & 0xff), (unsigned)(a & 0xff));
}

static int device_is_paired(const char *addr_s) {
  BLUETOOTH_DEVICE_SEARCH_PARAMS sp;
  BLUETOOTH_DEVICE_INFO di;
  HBLUETOOTH_DEVICE_FIND find;
  BTH_ADDR want;
  int paired = 0;
  if (!parse_addr(addr_s, &want)) return 0;
  memset(&sp, 0, sizeof(sp));
  sp.dwSize = sizeof(sp);
  sp.fReturnAuthenticated = TRUE;
  sp.fReturnRemembered = TRUE;
  memset(&di, 0, sizeof(di));
  di.dwSize = sizeof(di);
  find = BluetoothFindFirstDevice(&sp, &di);
  if (!find) return 0;
  do {
    if (di.Address.ullLong == want && di.fAuthenticated) { paired = 1; break; }
  } while (BluetoothFindNextDevice(find, &di));
  BluetoothFindDeviceClose(find);
  return paired;
}

/* ---- discovery ------------------------------------------------------ */

static DWORD WINAPI discover_thread(LPVOID arg) {
  BLUETOOTH_DEVICE_SEARCH_PARAMS sp;
  BLUETOOTH_DEVICE_INFO di;
  HBLUETOOTH_DEVICE_FIND find;
  char item[256], addr[32];
  (void)arg;
  EnterCriticalSection(&g_lock);
  strcpy(g_pairing_stage, "discovering");
  g_found[0] = 0;
  LeaveCriticalSection(&g_lock);

  memset(&sp, 0, sizeof(sp));
  sp.dwSize = sizeof(sp);
  sp.fReturnAuthenticated = TRUE;
  sp.fReturnRemembered = TRUE;
  sp.fReturnUnknown = TRUE;
  sp.fIssueInquiry = TRUE;
  sp.cTimeoutMultiplier = 6; /* ~7.7s inquiry */
  memset(&di, 0, sizeof(di));
  di.dwSize = sizeof(di);
  find = BluetoothFindFirstDevice(&sp, &di);
  if (find) {
    do {
      char name[128];
      format_addr(di.Address.ullLong, addr, sizeof(addr));
      WideCharToMultiByte(CP_UTF8, 0, di.szName, -1, name, sizeof(name), NULL, NULL);
      snprintf(item, sizeof(item), "%s{\"address\":\"%s\",\"name\":\"%s\"}",
               g_found[0] ? "," : "", addr, name[0] ? name : "Unknown device");
      EnterCriticalSection(&g_lock);
      if (strlen(g_found) + strlen(item) < sizeof(g_found) - 1)
        strcat(g_found, item);
      LeaveCriticalSection(&g_lock);
    } while (BluetoothFindNextDevice(find, &di));
    BluetoothFindDeviceClose(find);
  }
  EnterCriticalSection(&g_lock);
  if (!strcmp(g_pairing_stage, "discovering")) strcpy(g_pairing_stage, "idle");
  LeaveCriticalSection(&g_lock);
  logline("discovery: complete");
  return 0;
}

/* ---- pairing --------------------------------------------------------- */

static volatile HANDLE g_pair_reply_event = NULL;
static volatile int g_pair_accept = 0;
static BLUETOOTH_AUTHENTICATION_CALLBACK_PARAMS g_auth_params;

static BOOL CALLBACK auth_callback(LPVOID param,
                                   PBLUETOOTH_AUTHENTICATION_CALLBACK_PARAMS p) {
  BLUETOOTH_AUTHENTICATE_RESPONSE resp;
  (void)param;
  if (p->authenticationMethod == BLUETOOTH_AUTHENTICATION_METHOD_NUMERIC_COMPARISON) {
    EnterCriticalSection(&g_lock);
    snprintf(g_pairing_code, sizeof(g_pairing_code), "%06lu",
             (unsigned long)p->Numeric_Value);
    strcpy(g_pairing_stage, "confirm");
    g_auth_params = *p;
    LeaveCriticalSection(&g_lock);
    logline("pairing: confirm code %06lu (device is showing the same code)",
            (unsigned long)p->Numeric_Value);
    /* Wait for /pair/reply. */
    WaitForSingleObject(g_pair_reply_event, 60000);
    memset(&resp, 0, sizeof(resp));
    resp.authMethod = BLUETOOTH_AUTHENTICATION_METHOD_NUMERIC_COMPARISON;
    resp.bthAddressRemote = p->deviceInfo.Address;
    resp.negativeResponse = g_pair_accept ? FALSE : TRUE;
    BluetoothSendAuthenticationResponseEx(NULL, &resp);
    return TRUE;
  }
  return FALSE;
}

static DWORD WINAPI pair_thread(LPVOID arg) {
  char *addr_s = (char *)arg;
  BLUETOOTH_DEVICE_INFO di;
  HBLUETOOTH_AUTHENTICATION_REGISTRATION reg = NULL;
  BTH_ADDR addr;
  DWORD rc;

  if (!parse_addr(addr_s, &addr)) {
    EnterCriticalSection(&g_lock);
    strcpy(g_pairing_stage, "failed");
    strcpy(g_pairing_error, "bad address");
    LeaveCriticalSection(&g_lock);
    free(addr_s);
    return 0;
  }

  memset(&di, 0, sizeof(di));
  di.dwSize = sizeof(di);
  di.Address.ullLong = addr;

  /* A stale half-bond makes hosts abort right after encryption; clear it. */
  BluetoothRemoveDevice(&di.Address);

  EnterCriticalSection(&g_lock);
  strcpy(g_pairing_stage, "connecting");
  g_pairing_code[0] = 0;
  g_pairing_error[0] = 0;
  LeaveCriticalSection(&g_lock);

  BluetoothRegisterForAuthenticationEx(&di, &reg, auth_callback, NULL);
  rc = BluetoothAuthenticateDeviceEx(NULL, NULL, &di, NULL,
                                     MITMProtectionNotRequired);
  if (reg) BluetoothUnregisterAuthentication(reg);

  EnterCriticalSection(&g_lock);
  if (rc == ERROR_SUCCESS) {
    strcpy(g_pairing_stage, "done");
    g_pairing_code[0] = 0;
    strncpy(g_device_address, addr_s, sizeof(g_device_address) - 1);
    state_save();
    logline("pairing: finished OK");
  } else {
    strcpy(g_pairing_stage, "failed");
    snprintf(g_pairing_error, sizeof(g_pairing_error), "pairing failed (%lu)", rc);
    logline("pairing: failed (%lu)", rc);
  }
  LeaveCriticalSection(&g_lock);
  free(addr_s);
  return 0;
}

/* ---- RFCOMM tunnel ---------------------------------------------------- */

typedef struct {
  SOCKET rf;
  SOCKET conns[MAX_STREAMS];
  unsigned ids[MAX_STREAMS];
  CRITICAL_SECTION wlock;
  volatile int dead;
  volatile time_t last_pong;
  volatile unsigned char peer_version;   /* 1 until a HELLO says otherwise */
  volatile unsigned peer_caps;
  volatile unsigned next_sid;
} Tunnel;

static int tunnel_supports_inbound(Tunnel *t) {
  return t && t->peer_version >= 2 && (t->peer_caps & CAP_INBOUND);
}

static int service_is_known(const char *name) {
  int i;
  for (i = 0; DEVICE_SERVICES[i]; i++)
    if (!strcmp(DEVICE_SERVICES[i], name)) return 1;
  return 0;
}

static Tunnel *g_tun = NULL;

static void tunnel_send(Tunnel *t, unsigned char type, unsigned sid,
                        const char *payload, unsigned len) {
  char hdr[7];
  hdr[0] = (char)type;
  hdr[1] = (char)(sid >> 24); hdr[2] = (char)(sid >> 16);
  hdr[3] = (char)(sid >> 8);  hdr[4] = (char)sid;
  hdr[5] = (char)(len >> 8);  hdr[6] = (char)len;
  EnterCriticalSection(&t->wlock);
  if (send(t->rf, hdr, 7, 0) != 7 ||
      (len && send(t->rf, payload, (int)len, 0) != (int)len))
    t->dead = 1;
  LeaveCriticalSection(&t->wlock);
}

static int slot_for(Tunnel *t, unsigned sid, int alloc) {
  int i, free_i = -1;
  for (i = 0; i < MAX_STREAMS; i++) {
    if (t->conns[i] != INVALID_SOCKET && t->ids[i] == sid) return i;
    if (alloc && t->conns[i] == INVALID_SOCKET && free_i < 0) free_i = i;
  }
  return alloc ? free_i : -1;
}

typedef struct { Tunnel *t; int slot; } PumpArg;

static DWORD WINAPI stream_pump(LPVOID argp) {
  PumpArg *arg = (PumpArg *)argp;
  Tunnel *t = arg->t;
  int slot = arg->slot;
  SOCKET c = t->conns[slot];
  unsigned sid = t->ids[slot];
  char buf[CHUNK];
  int n;
  free(arg);
  while (!t->dead && (n = recv(c, buf, sizeof(buf), 0)) > 0)
    tunnel_send(t, 2, sid, buf, (unsigned)n);
  if (t->conns[slot] == c) {
    closesocket(c);
    t->conns[slot] = INVALID_SOCKET;
    tunnel_send(t, 3, sid, NULL, 0);
  }
  return 0;
}

static void tunnel_open_stream(Tunnel *t, unsigned sid) {
  struct sockaddr_in sa;
  SOCKET c;
  int slot = slot_for(t, sid, 1);
  if (slot < 0) { tunnel_send(t, 3, sid, NULL, 0); return; }
  c = socket(AF_INET, SOCK_STREAM, 0);
  memset(&sa, 0, sizeof(sa));
  sa.sin_family = AF_INET;
  sa.sin_addr.s_addr = inet_addr("127.0.0.1");
  sa.sin_port = htons(SERVER_PORT);
  if (connect(c, (struct sockaddr *)&sa, sizeof(sa)) != 0) {
    closesocket(c);
    tunnel_send(t, 3, sid, NULL, 0);
    return;
  }
  t->conns[slot] = c;
  t->ids[slot] = sid;
  {
    PumpArg *arg = (PumpArg *)malloc(sizeof(PumpArg));
    arg->t = t; arg->slot = slot;
    CloseHandle(CreateThread(NULL, 0, stream_pump, arg, 0, NULL));
  }
}

/* Announce our version/caps and hand the device our clock — it has no RTC,
 * and a wrong clock breaks TLS from the device in confusing ways. */
static void tunnel_send_hello(Tunnel *t) {
  char p[11];
  unsigned long long epoch = (unsigned long long)time(NULL);
  int i;
  p[0] = (char)PROTOCOL_VERSION;
  p[1] = (char)((OUR_CAPS >> 8) & 0xff);
  p[2] = (char)(OUR_CAPS & 0xff);
  for (i = 0; i < 8; i++) p[3 + i] = (char)((epoch >> (8 * (7 - i))) & 0xff);
  tunnel_send(t, 6, 0, p, sizeof(p));
}

/* Allocate in our half of the id space; wraps inside the high half so it can
 * never stray into the device's. */
static unsigned tunnel_alloc_sid(Tunnel *t) {
  unsigned n;
  EnterCriticalSection(&t->wlock);
  t->next_sid = (t->next_sid + 1) & 0x7FFFFFFFu;
  if (t->next_sid == 0) t->next_sid = 1;
  n = t->next_sid | NS_MASK;
  LeaveCriticalSection(&t->wlock);
  return n;
}

/* The device pings every 5s and drops the link after 15s of silence, so
 * answering PING is mandatory — without it the link cannot survive 15
 * seconds. We also ping outward so a half-open link (reports connected,
 * passes no data) gets torn down here rather than lingering. */
static DWORD WINAPI heartbeat_thread(LPVOID arg) {
  Tunnel *t = (Tunnel *)arg;
  while (!t->dead) {
    Sleep(5000);
    if (t->dead) break;
    tunnel_send(t, 4, 0, NULL, 0);
    if (difftime(time(NULL), t->last_pong) > 15) {
      logline("heartbeat: no pong in 15s - link dead, closing");
      t->dead = 1;
      shutdown(t->rf, SD_BOTH);
      break;
    }
  }
  return 0;
}

static void tunnel_run(Tunnel *t) {
  char buf[8192], frame[CHUNK + 16];
  unsigned have = 0;
  int n, i;
  (void)frame;
  while (!t->dead) {
    n = recv(t->rf, buf + have, (int)(sizeof(buf) - have), 0);
    if (n <= 0) break;
    have += (unsigned)n;
    for (;;) {
      unsigned char type; unsigned sid, len;
      if (have < 7) break;
      type = (unsigned char)buf[0];
      sid = ((unsigned char)buf[1] << 24) | ((unsigned char)buf[2] << 16) |
            ((unsigned char)buf[3] << 8) | (unsigned char)buf[4];
      len = ((unsigned char)buf[5] << 8) | (unsigned char)buf[6];
      if (have < 7 + len) break;
      if (type == 1) {
        tunnel_open_stream(t, sid);
      } else if (type == 2) {
        int slot = slot_for(t, sid, 0);
        if (slot >= 0) send(t->conns[slot], buf + 7, (int)len, 0);
      } else if (type == 3) {
        int slot = slot_for(t, sid, 0);
        if (slot >= 0) { closesocket(t->conns[slot]); t->conns[slot] = INVALID_SOCKET; }
      } else if (type == 4) {
        tunnel_send(t, 5, 0, NULL, 0);   /* PING -> PONG */
      } else if (type == 5) {
        t->last_pong = time(NULL);       /* PONG */
      } else if (type == 6) {            /* HELLO */
        if (len >= 11) {
          t->peer_version = (unsigned char)buf[7];
          t->peer_caps = ((unsigned char)buf[8] << 8) | (unsigned char)buf[9];
          logline("device speaks v%u caps=0x%04x",
                  (unsigned)t->peer_version, t->peer_caps);
        }
      } else if (type == 7) {            /* OPEN_ACK for a stream we opened */
        if (len >= 1 && (unsigned char)buf[7] != ACK_OK) {
          int slot = slot_for(t, sid, 0);
          logline("device refused stream %u (code %u)", sid,
                  (unsigned)(unsigned char)buf[7]);
          if (slot >= 0) {
            closesocket(t->conns[slot]);
            t->conns[slot] = INVALID_SOCKET;
          }
        }
      }
      /* Any other type is from a newer peer: skip the frame. Its bytes are
       * already consumed below, so the streams that follow stay intact. */
      memmove(buf, buf + 7 + len, have - 7 - len);
      have -= 7 + len;
    }
  }
  t->dead = 1;
  for (i = 0; i < MAX_STREAMS; i++)
    if (t->conns[i] != INVALID_SOCKET) { closesocket(t->conns[i]); t->conns[i] = INVALID_SOCKET; }
}

/* ---- device service forwarding -----------------------------------------
 *
 * Exposes a named service on the DEVICE as a plain TCP port here, so ordinary
 * tools work unmodified — point chrome://inspect at the forwarded port and you
 * are debugging the Car Thing over Bluetooth, no cable. */

typedef struct {
  char name[32];
  SOCKET listener;
  unsigned short port;
  volatile int stop;
} Forward;

static Forward g_forwards[MAX_FORWARDS];

/* Open a stream to a named device service, bridged to a local connection. */
static void tunnel_open_device_service(Tunnel *t, const char *name, SOCKET local) {
  unsigned sid = tunnel_alloc_sid(t);
  int slot = slot_for(t, sid, 1);
  size_t n = strlen(name);
  char payload[34];
  PumpArg *arg;
  if (slot < 0 || n > 32) { closesocket(local); return; }
  t->conns[slot] = local;
  t->ids[slot] = sid;
  payload[0] = (char)KIND_SERVICE;
  payload[1] = (char)n;
  memcpy(payload + 2, name, n);
  tunnel_send(t, 1, sid, payload, (unsigned)(n + 2));
  arg = (PumpArg *)malloc(sizeof(PumpArg));
  arg->t = t;
  arg->slot = slot;
  CloseHandle(CreateThread(NULL, 0, stream_pump, arg, 0, NULL));
}

static DWORD WINAPI forward_accept_thread(LPVOID argp) {
  Forward *f = (Forward *)argp;
  while (!f->stop) {
    SOCKET client = accept(f->listener, NULL, NULL);
    if (client == INVALID_SOCKET) break;
    if (!g_tun || !tunnel_supports_inbound(g_tun)) { closesocket(client); continue; }
    tunnel_open_device_service(g_tun, f->name, client);
  }
  closesocket(f->listener);
  f->listener = INVALID_SOCKET;
  return 0;
}

/* Returns the local port, or 0 with *err set. */
static unsigned short forward_open(const char *name, const char **err) {
  struct sockaddr_in sa;
  int len = sizeof(sa), i, free_i = -1;
  SOCKET srv;
  for (i = 0; i < MAX_FORWARDS; i++) {
    if (g_forwards[i].listener != INVALID_SOCKET && !strcmp(g_forwards[i].name, name))
      return g_forwards[i].port;
    if (g_forwards[i].listener == INVALID_SOCKET && free_i < 0) free_i = i;
  }
  if (!service_is_known(name)) { *err = "unknown service"; return 0; }
  if (!g_tun || !tunnel_supports_inbound(g_tun)) {
    *err = "device does not support inbound streams";
    return 0;
  }
  if (free_i < 0) { *err = "too many forwards"; return 0; }

  srv = socket(AF_INET, SOCK_STREAM, 0);
  memset(&sa, 0, sizeof(sa));
  sa.sin_family = AF_INET;
  /* Loopback only: this is a doorway into the device and must not be
   * reachable from the network. */
  sa.sin_addr.s_addr = inet_addr("127.0.0.1");
  sa.sin_port = 0;
  if (bind(srv, (struct sockaddr *)&sa, sizeof(sa)) != 0 || listen(srv, 4) != 0) {
    closesocket(srv);
    *err = "could not bind a local port";
    return 0;
  }
  len = sizeof(sa);
  getsockname(srv, (struct sockaddr *)&sa, &len);

  strncpy(g_forwards[free_i].name, name, sizeof(g_forwards[free_i].name) - 1);
  g_forwards[free_i].listener = srv;
  g_forwards[free_i].port = ntohs(sa.sin_port);
  g_forwards[free_i].stop = 0;
  CloseHandle(CreateThread(NULL, 0, forward_accept_thread, &g_forwards[free_i], 0, NULL));
  logline("forward: device '%s' available on 127.0.0.1:%u", name,
          (unsigned)g_forwards[free_i].port);
  return g_forwards[free_i].port;
}

/* Rendered into /status so the UI knows what is reachable. Static buffers are
 * fine here: only the control thread builds these. */
static const char *services_json(void) {
  static char buf[256];
  int i;
  buf[0] = 0;
  if (!tunnel_supports_inbound(g_tun)) return buf;
  for (i = 0; DEVICE_SERVICES[i]; i++) {
    char item[64];
    snprintf(item, sizeof(item), "%s{\"name\":\"%s\"}",
             buf[0] ? "," : "", DEVICE_SERVICES[i]);
    if (strlen(buf) + strlen(item) < sizeof(buf) - 1) strcat(buf, item);
  }
  return buf;
}

static const char *forwards_json(void) {
  static char buf[256];
  int i;
  buf[0] = 0;
  for (i = 0; i < MAX_FORWARDS; i++) {
    char item[96];
    if (g_forwards[i].listener == INVALID_SOCKET) continue;
    snprintf(item, sizeof(item), "%s{\"service\":\"%s\",\"port\":%u}",
             buf[0] ? "," : "", g_forwards[i].name, (unsigned)g_forwards[i].port);
    if (strlen(buf) + strlen(item) < sizeof(buf) - 1) strcat(buf, item);
  }
  return buf;
}

static void forward_close(const char *name) {
  int i;
  for (i = 0; i < MAX_FORWARDS; i++) {
    if (g_forwards[i].listener != INVALID_SOCKET &&
        (!name || !strcmp(g_forwards[i].name, name))) {
      g_forwards[i].stop = 1;
      closesocket(g_forwards[i].listener);
      g_forwards[i].listener = INVALID_SOCKET;
      g_forwards[i].port = 0;
      logline("forward: stopped '%s'", g_forwards[i].name);
    }
  }
}

/* ---- control API ------------------------------------------------------- */

static void control_respond(SOCKET c, const char *req) {
  char body[4096], out[8192], val[64];
  const char *json = strstr(req, "\r\n\r\n");
  json = json ? json + 4 : "";

  if (!strncmp(req, "POST /preference", 16)) {
    if (json_str(json, "preference", val, sizeof(val)) &&
        (!strcmp(val, "bluetooth") || !strcmp(val, "usb"))) {
      EnterCriticalSection(&g_lock);
      strcpy(g_preference, val);
      state_save();
      LeaveCriticalSection(&g_lock);
      logline("preference set to %s by UI", val);
      if (!strcmp(val, "usb")) fall_back_to_usb();
      else if (g_link_up) prefer_bluetooth();
    }
  } else if (!strncmp(req, "POST /discover", 14)) {
    CloseHandle(CreateThread(NULL, 0, discover_thread, NULL, 0, NULL));
  } else if (!strncmp(req, "POST /pair/reply", 16)) {
    g_pair_accept = strstr(json, "true") != NULL;
    EnterCriticalSection(&g_lock);
    strcpy(g_pairing_stage, g_pair_accept ? "finishing" : "failed");
    if (!g_pair_accept) strcpy(g_pairing_error, "rejected");
    LeaveCriticalSection(&g_lock);
    if (g_pair_reply_event) SetEvent(g_pair_reply_event);
  } else if (!strncmp(req, "POST /pair", 10)) {
    if (json_str(json, "address", val, sizeof(val)))
      CloseHandle(CreateThread(NULL, 0, pair_thread, _strdup(val), 0, NULL));
  } else if (!strncmp(req, "POST /unpair", 12)) {
    if (json_str(json, "address", val, sizeof(val))) {
      BTH_ADDR a;
      if (parse_addr(val, &a)) {
        BLUETOOTH_ADDRESS ba;
        ba.ullLong = a;
        BluetoothRemoveDevice(&ba);
        logline("unpair: removed bond for %s", val);
        EnterCriticalSection(&g_lock);
        if (!_stricmp(val, g_device_address)) { g_device_address[0] = 0; state_save(); }
        LeaveCriticalSection(&g_lock);
      }
    }
  } else if (!strncmp(req, "POST /device", 12)) {
    if (json_str(json, "address", val, sizeof(val))) {
      EnterCriticalSection(&g_lock);
      strncpy(g_device_address, val, sizeof(g_device_address) - 1);
      state_save();
      LeaveCriticalSection(&g_lock);
      logline("device address set to %s by UI", val);
    }
  } else if (!strncmp(req, "POST /forward/open", 18)) {
    char small[256];
    const char *err = NULL;
    unsigned short port = 0;
    if (json_str(json, "service", val, sizeof(val)))
      port = forward_open(val, &err);
    else
      err = "missing service";
    if (err)
      snprintf(small, sizeof(small), "{\"ok\":false,\"error\":\"%s\"}", err);
    else
      snprintf(small, sizeof(small),
               "{\"ok\":true,\"service\":\"%s\",\"port\":%u}", val, (unsigned)port);
    snprintf(out, sizeof(out),
             "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
             "Content-Length: %u\r\nConnection: close\r\n\r\n%s",
             (unsigned)strlen(small), small);
    send(c, out, (int)strlen(out), 0);
    return;
  } else if (!strncmp(req, "POST /forward/close", 19)) {
    const char *body_ok = "{\"ok\":true}";
    if (json_str(json, "service", val, sizeof(val))) forward_close(val);
    snprintf(out, sizeof(out),
             "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
             "Content-Length: %u\r\nConnection: close\r\n\r\n%s",
             (unsigned)strlen(body_ok), body_ok);
    send(c, out, (int)strlen(out), 0);
    return;
  }

  EnterCriticalSection(&g_lock);
  snprintf(body, sizeof(body),
           "{\"preference\":\"%s\",\"transport\":\"%s\",\"linkUp\":%s,"
           "\"deviceAddress\":%s%s%s,\"paired\":%s,"
           "\"pairing\":{\"stage\":\"%s\",\"code\":%s%s%s,\"error\":%s%s%s},"
           "\"found\":[%s],"
           "\"protocol\":{\"version\":%d,\"inbound\":%s},"
           "\"services\":[%s],\"forwards\":[%s]}",
           g_preference, active_transport(), g_link_up ? "true" : "false",
           g_device_address[0] ? "\"" : "", g_device_address[0] ? g_device_address : "null",
           g_device_address[0] ? "\"" : "",
           g_device_address[0] && device_is_paired(g_device_address) ? "true" : "false",
           g_pairing_stage,
           g_pairing_code[0] ? "\"" : "", g_pairing_code[0] ? g_pairing_code : "null",
           g_pairing_code[0] ? "\"" : "",
           g_pairing_error[0] ? "\"" : "", g_pairing_error[0] ? g_pairing_error : "null",
           g_pairing_error[0] ? "\"" : "",
           g_found,
           PROTOCOL_VERSION,
           tunnel_supports_inbound(g_tun) ? "true" : "false",
           services_json(), forwards_json());
  LeaveCriticalSection(&g_lock);

  snprintf(out, sizeof(out),
           "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
           "Access-Control-Allow-Origin: *\r\n"
           "Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n"
           "Access-Control-Allow-Headers: Content-Type\r\n"
           "Cache-Control: no-store\r\nContent-Length: %u\r\n"
           "Connection: close\r\n\r\n%s",
           (unsigned)strlen(body), body);
  send(c, out, (int)strlen(out), 0);
}

static DWORD WINAPI control_thread(LPVOID arg) {
  SOCKET srv = socket(AF_INET, SOCK_STREAM, 0);
  struct sockaddr_in sa;
  int one = 1;
  (void)arg;
  setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, (const char *)&one, sizeof(one));
  memset(&sa, 0, sizeof(sa));
  sa.sin_family = AF_INET;
  sa.sin_addr.s_addr = inet_addr("127.0.0.1");
  sa.sin_port = htons(CONTROL_PORT);
  if (bind(srv, (struct sockaddr *)&sa, sizeof(sa)) != 0 || listen(srv, 8) != 0) {
    logline("control: could not bind 127.0.0.1:%d", CONTROL_PORT);
    return 1;
  }
  logline("control: listening on 127.0.0.1:%d", CONTROL_PORT);
  for (;;) {
    SOCKET c = accept(srv, NULL, NULL);
    char req[16384];
    int n;
    if (c == INVALID_SOCKET) continue;
    n = recv(c, req, sizeof(req) - 1, 0);
    if (n > 0) {
      req[n] = 0;
      control_respond(c, req);
    }
    closesocket(c);
  }
}

/* ---- main loop ----------------------------------------------------------- */

int main(void) {
  WSADATA wsa;
  WSAStartup(MAKEWORD(2, 2), &wsa);
  InitializeCriticalSection(&g_lock);
  g_pair_reply_event = CreateEventA(NULL, FALSE, FALSE, NULL);
  state_path_init();
  state_load();
  /* Static storage zero-initializes, and 0 is a VALID socket value — the
   * forward table must start as explicitly-empty or an unopened slot looks
   * like a live listener. */
  {
    int k;
    for (k = 0; k < MAX_FORWARDS; k++) {
      g_forwards[k].listener = INVALID_SOCKET;
      g_forwards[k].port = 0;
    }
  }
  logline("state loaded: preference=%s device=%s", g_preference,
          g_device_address[0] ? g_device_address : "unset");

  CloseHandle(CreateThread(NULL, 0, control_thread, NULL, 0, NULL));
  logline("bluetooth bridge loop up");

  for (;;) {
    SOCKADDR_BTH sab;
    SOCKET rf;
    BTH_ADDR addr;
    int i;

    if (!strcmp(g_preference, "usb")) {
      if (g_link_up) g_link_up = 0;
      fall_back_to_usb();
      Sleep(5000);
      continue;
    }
    if (!strcmp(g_pairing_stage, "discovering") || !strcmp(g_pairing_stage, "connecting") ||
        !strcmp(g_pairing_stage, "confirm") || !strcmp(g_pairing_stage, "finishing")) {
      Sleep(1000);
      continue;
    }
    if (!g_device_address[0] || !parse_addr(g_device_address, &addr)) {
      Sleep(3000);
      continue;
    }

    rf = socket(AF_BTH, SOCK_STREAM, BTHPROTO_RFCOMM);
    memset(&sab, 0, sizeof(sab));
    sab.addressFamily = AF_BTH;
    sab.btAddr = addr;
    sab.port = RFCOMM_CHANNEL;
    logline("connecting to %s rfcomm ch%d...", g_device_address, RFCOMM_CHANNEL);
    if (connect(rf, (struct sockaddr *)&sab, sizeof(sab)) != 0) {
      logline("connect failed (%d); device off/out of range? retrying in 10s",
              WSAGetLastError());
      closesocket(rf);
      g_link_up = 0;
      fall_back_to_usb();
      Sleep(10000);
      continue;
    }

    logline("rfcomm open");
    g_link_up = 1;
    prefer_bluetooth();
    {
      Tunnel t;
      memset(&t, 0, sizeof(t));
      t.rf = rf;
      for (i = 0; i < MAX_STREAMS; i++) t.conns[i] = INVALID_SOCKET;
      InitializeCriticalSection(&t.wlock);
      t.last_pong = time(NULL);
      t.peer_version = 1;   /* until a HELLO says otherwise */
      t.peer_caps = 0;
      t.next_sid = 0;
      g_tun = &t;
      CloseHandle(CreateThread(NULL, 0, heartbeat_thread, &t, 0, NULL));
      /* A v1 device ignores the unknown frame type and keeps working. */
      tunnel_send_hello(&t);
      tunnel_run(&t);
      /* The streams behind any forwarded ports died with the link. */
      forward_close(NULL);
      g_tun = NULL;
      DeleteCriticalSection(&t.wlock);
    }
    closesocket(rf);
    logline("session ended");
    g_link_up = 0;
    fall_back_to_usb();
    Sleep(10000);
  }
}
