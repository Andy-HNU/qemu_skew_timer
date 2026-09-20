/* Experiment-only recorder, included by generated skew.c. No production edit. */
#define EXP5_CAPACITY 600000
typedef struct Exp5Sample {
    int64_t host, model, visible;
    uint64_t global, window, slope, ticks;
    int cpu;
    const char *event;
} Exp5Sample;
static Exp5Sample exp5_samples[EXP5_CAPACITY];
static unsigned exp5_used;
static unsigned exp5_dropped, exp5_unlocked;
static const char *exp5_path;
static unsigned exp5_delay_us;
static __thread bool exp5_in_counter;
static __thread Exp5Sample exp5_pending;
void skew_exp5_begin(void);
uint64_t skew_exp5_end(uint64_t ticks);

static void exp5_append(Exp5Sample sample)
{
    if (!exp5_path) {
        return;
    }
    if (!bql_locked()) {
        exp5_unlocked++;
        return;
    }
    if (exp5_used == EXP5_CAPACITY) {
        exp5_dropped++;
        return;
    }
    exp5_samples[exp5_used++] = sample;
}

static void exp5_event(const char *event)
{
    Exp5Sample sample = {
        .host = cpu_get_clock(),
        .model = qatomic_read_i64(&model_ns),
        .visible = qatomic_read_i64(&visible_ns),
        .global = global_icount,
        .window = window_ns,
        .slope = qatomic_read_u64(&visible_slope_q32),
        .cpu = current_cpu ? current_cpu->cpu_index : 0,
        .event = event,
    };
    exp5_append(sample);
}

void skew_exp5_begin(void)
{
    /* Explicit fault injection only; holds the existing BQL across read delay. */
    if (exp5_delay_us) {
        g_usleep(exp5_delay_us);
    }
    exp5_in_counter = true;
    memset(&exp5_pending, 0, sizeof(exp5_pending));
}

uint64_t skew_exp5_end(uint64_t ticks)
{
    exp5_in_counter = false;
    exp5_pending.ticks = ticks;
    if (exp5_pending.event) {
        exp5_append(exp5_pending);
    }
    return ticks;
}

static void exp5_dump(void)
{
    FILE *f = fopen(exp5_path, "w");
    unsigned i;
    if (!f) {
        perror("exp5 output");
        return;
    }
    fprintf(f, "evidence,source_log,run_id,seq,host_ns,cpu,global_icount,"
            "model_ns,visible_ns,window_ns,slope_q32,event,counter_ticks\n");
    for (i = 0; i < exp5_used; i++) {
        Exp5Sample *s = &exp5_samples[i];
        fprintf(f, "measured,probe-memory-buffer,run,%u,%"PRId64",%d,"
                "%"PRIu64",%"PRId64",%"PRId64",%"PRIu64",%"PRIu64",%s,%"PRIu64"\n",
                i, s->host, s->cpu, s->global, s->model, s->visible,
                s->window, s->slope, s->event, s->ticks);
    }
    fclose(f);
    fprintf(stderr, "EXP5_RECORDER samples=%u dropped=%u unlocked=%u\n",
            exp5_used, exp5_dropped, exp5_unlocked);
}

static void exp5_init(void)
{
    exp5_path = getenv("SKEW_EXP5_TRACE");
    if (getenv("SKEW_EXP5_READ_DELAY_US")) {
        exp5_delay_us = atoi(getenv("SKEW_EXP5_READ_DELAY_US"));
    }
    if (exp5_path) {
        atexit(exp5_dump);
    }
}
