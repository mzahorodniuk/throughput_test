// ===========================================================================
//  CAN load generator for the SCIS FPGA throughput test
//
//  This board is NOT the device under test. It is a traffic source: it emits
//  classic CAN frames at a rate the host commands over the serial port, and the
//  host counts how many of them come back out of the FPGA over Ethernet.
//
//  Each frame carries a sequence number in its 4-byte Q22.10 payload, so the
//  host can tell exactly which frames went missing. `failed` (below) is the
//  other half of that story: frames we never got onto the bus are our fault,
//  not the FPGA's, and must not be counted as FPGA loss.
//
//  Board: Nucleo-F446RE. The F446 has Bosch **bxCAN**, not the H7's FDCAN, so
//  the peripheral layer here is HAL_CAN with three fixed TX mailboxes in place
//  of a TX FIFO in message RAM. The frames on the wire are identical.
//
//  Wiring -- config (A), two wires, matching top.vhd:471-479 as built:
//    PB9 = CAN1_TX (AF9, push-pull 3V3)  ->  FPGA can_rx = PMOD B pin 7
//    PB8 = CAN1_RX (AF9, direct input)   <-  FPGA can_tx = PMOD B pin 8
//    GND = common with the FPGA board (mandatory)
//  PB9/PB8 are D14/D15 on the Arduino header (CN5 pins 9 and 10). The H743 used
//  PD1/PD0; port D is not bonded out on the F446RE's LQFP64 package, so CAN1 is
//  on PA12/PA11 or PB9/PB8 and this picks the pair on the Arduino header.
//
//  CAN ID 2 == DeviceTypeID 2 == Room_Humidity (20..80 %RH). The FPGA maps
//  metric_id <= rx_id(9 downto 0) & "000000", so ID 2 lands on identifier
//  0x0080 and returns to the host on UDP 9218 (CAN_TUSER => "001").
//
//  Serial protocol at 115200, one command per line:
//      RATE <fps>   start sending at <fps> frames/s   -> "OK RATE <fps>"
//      STOP         stop sending                      -> "OK STOP"
//      STAT         counters since the last RATE      -> "STAT sent=.. failed=.. tec=.. rec=.."
//      PING         liveness check                    -> "PONG"
//
//  The CAN setup is lifted from the working can_publisher firmware: 125 kbit/s
//  at a 75% sample point, matching the FPGA's can_node. Three things differ, all
//  for throughput:
//    * all three TX mailboxes are used, not one send at a time
//    * the send is non-blocking; no free mailbox increments `failed` instead of
//      busy-waiting 20 ms and silently distorting the rate
//    * no delay(), no period clamp, no sweep, no ACK probe
//
//  There is no ACK probe here, so a dead return wire would be invisible during a
//  ramp -- it would just look like FPGA loss. Instead can_init() prints a one-shot
//  `# rx wire (PB8): ... CONNECTED|OPEN!` verdict at boot; see report_rx_wire.
// ===========================================================================

#include <Arduino.h>
#include <stm32f4xx_hal.h>

#define BUS_TWO_WIRE     1
#define BUS_WIRED_AND    2
#define BUS_TRANSCEIVER  3

#ifndef BUS_WIRING
#define BUS_WIRING BUS_TWO_WIRE
#endif

#if   BUS_WIRING == BUS_TWO_WIRE
  // CAN_MODE_LOOPBACK: still drives the TX pin (CAN_MODE_SILENT_LOOPBACK is the
  // internal one), and the FPGA echoes the bus (ECHO_RX=true) so the controller's
  // bit monitoring sees its own bits. One-shot -- a lost arbitration is dropped,
  // never retried, which keeps the offered rate exactly what we asked for.
  #define PUBLISH_MODE        CAN_MODE_LOOPBACK
  #define AUTO_RETRANSMIT     DISABLE
  #define TX_PIN_MODE         GPIO_MODE_AF_PP
  #define BUS_NAME            "two wires, no transceiver (config A)"
#elif BUS_WIRING == BUS_WIRED_AND
  #define PUBLISH_MODE        CAN_MODE_NORMAL
  #define AUTO_RETRANSMIT     ENABLE
  #define TX_PIN_MODE         GPIO_MODE_AF_OD
  #define BUS_NAME            "one shared wire + pull-up (config B)"
#elif BUS_WIRING == BUS_TRANSCEIVER
  #define PUBLISH_MODE        CAN_MODE_NORMAL
  #define AUTO_RETRANSMIT     ENABLE
  #define TX_PIN_MODE         GPIO_MODE_AF_PP
  #define BUS_NAME            "TJA1051T/3 transceivers"
#else
  #error "BUS_WIRING must be BUS_TWO_WIRE, BUS_WIRED_AND or BUS_TRANSCEIVER"
#endif

// ---------------------------------------------------------------------------
//  Pins and bit timing.
//
//  bxCAN counts time quanta off APB1, not off an FDCAN-style kernel clock. The
//  Arduino core's F446RE tree is SYSCLK 180 MHz, APB1 = HCLK/4 = 45 MHz, and
//  45 MHz / 125 kbit/s = 360 tq per bit-time to divide up:
//      20 tq/bit x prescaler 18 = 360   -> exactly 125 kbit/s
//      1 (sync) + 14 (Seg1) + 5 (Seg2) = 20 tq, sample point (1+14)/20 = 75.0%
//  The H743 build used 16 tq/bit, which cannot divide 360 evenly (22.5), so the
//  segment split changed but the 75% sample point did not.
//
//  The stock clock tree runs off the internal HSI RC (~1%), where the H743ZI
//  variant used the ST-LINK's crystal-derived 8 MHz MCO. Classic CAN here
//  tolerates about 0.77% total error between the two nodes, so if a ramp shows
//  stuff/form/CRC errors rather than clean saturation, that oscillator is the
//  first suspect -- can_publisher's -DUSE_HSE_MCO_CLOCK block is the fix.
// ---------------------------------------------------------------------------
#define CAN_GPIO_PORT     GPIOB
#define CAN_GPIO_CLK_EN() __HAL_RCC_GPIOB_CLK_ENABLE()
#define CAN_TX_PIN        GPIO_PIN_9    // D14, -> FPGA can_rx
#define CAN_RX_PIN        GPIO_PIN_8    // D15, <- FPGA can_tx
#define CAN_GPIO_AF       GPIO_AF9_CAN1

static const uint32_t CAN_BITRATE    = 125000;
static const uint32_t CAN_TQ_PER_BIT = 20;
#define CAN_SEG1  CAN_BS1_14TQ
#define CAN_SEG2  CAN_BS2_5TQ
#define CAN_SJW   CAN_SJW_4TQ

// ---------------------------------------------------------------------------
//  Sequence encoding -- MUST match CAN_SEQ_MIN / CAN_SEQ_SPAN in host/scis.py.
//  Room_Humidity's critical band is 20..80 %RH; staying inside 21..79 means no
//  frame can trip the interlock, which would latch until SW3 and poison the run.
// ---------------------------------------------------------------------------
static const uint16_t CAN_ID       = 2;
static const int32_t  Q_SCALE      = 1024;          // Q22.10
static const int32_t  SEQ_MIN      = 21 * Q_SCALE;  // 21.0 %RH
static const int32_t  SEQ_SPAN     = (79 - 21) * Q_SCALE;   // 59392 values

static CAN_HandleTypeDef hcan1;

// Run state
static bool     running       = false;
static uint32_t interval_us   = 0;
static uint32_t next_due_us   = 0;
static uint32_t seq           = 0;
static uint32_t sent_count    = 0;
static uint32_t failed_count  = 0;

static char     line[64];
static uint8_t  line_len = 0;


static void can_fatal(const char *where) {
    Serial.print("FATAL: CAN ");
    Serial.println(where);
    while (1) { delay(1000); }
}

static bool can_config_filter(void) {
    // One wide-open 32-bit mask filter into RXFIFO0. We do not read frames, but
    // an unconfigured filter bank makes the peripheral reject everything
    // including its own loopback, which upsets the status readback.
    CAN_FilterTypeDef f = {0};
    f.FilterBank           = 0;
    f.FilterMode           = CAN_FILTERMODE_IDMASK;
    f.FilterScale          = CAN_FILTERSCALE_32BIT;
    f.FilterIdHigh         = 0x0000;
    f.FilterIdLow          = 0x0000;
    f.FilterMaskIdHigh     = 0x0000;   // mask 0 = accept every ID
    f.FilterMaskIdLow      = 0x0000;
    f.FilterFIFOAssignment = CAN_RX_FIFO0;
    f.FilterActivation     = CAN_FILTER_ENABLE;
    // The F446 has CAN1+CAN2 sharing 28 banks; banks below this one are CAN1's.
    f.SlaveStartFilterBank = 14;
    return HAL_CAN_ConfigFilter(&hcan1, &f) == HAL_OK;
}

// Is the return wire actually there? A CAN line idles recessive, so under the
// pull-up an OPEN pin and a healthy idle one both read high -- indistinguishable.
// Under a pull-DOWN they separate: a wire really connected to the FPGA's push-pull
// can_tx still reads high (a real driver beats the ~40k internal resistor), while
// an open wire collapses to low. Worth one line at boot, because an open return
// wire otherwise shows up as an unexplained CAN gap halfway through a ramp.
//
// All output is '#'-prefixed, which host/nucleo.py skips as a comment.
static void report_rx_wire(void) {
    GPIO_InitTypeDef g = {0};
    g.Pin   = CAN_RX_PIN;
    g.Mode  = GPIO_MODE_INPUT;
    g.Speed = GPIO_SPEED_FREQ_VERY_HIGH;

    g.Pull = GPIO_PULLDOWN;
    HAL_GPIO_Init(CAN_GPIO_PORT, &g);
    delay(2);                      // let ~40k charge the pad capacitance
    bool with_pulldown = HAL_GPIO_ReadPin(CAN_GPIO_PORT, CAN_RX_PIN) == GPIO_PIN_SET;

    g.Pull = GPIO_PULLUP;
    HAL_GPIO_Init(CAN_GPIO_PORT, &g);
    delay(2);
    bool with_pullup = HAL_GPIO_ReadPin(CAN_GPIO_PORT, CAN_RX_PIN) == GPIO_PIN_SET;

    g.Mode      = TX_PIN_MODE;     // hand the pad back to the CAN peripheral
    g.Pull      = GPIO_PULLUP;
    g.Alternate = CAN_GPIO_AF;
    HAL_GPIO_Init(CAN_GPIO_PORT, &g);

    Serial.print("# rx wire (PB8): pulldown=");
    Serial.print(with_pulldown ? "high" : "low");
    Serial.print(" pullup=");
    Serial.print(with_pullup ? "high" : "low");
    if (with_pulldown && with_pullup)
        Serial.println(" -- CONNECTED, FPGA is driving the line recessive");
    else if (!with_pulldown && with_pullup)
        Serial.println(" -- OPEN! wire PMOD B pin 8 -> PB8 (D15), else every ramp"
                       " reports FPGA loss that is really a missing wire");
    else
        Serial.println(" -- HELD LOW: shorted to ground, or the FPGA is parked dominant");
}

static void can_init(void) {
    __HAL_RCC_CAN1_CLK_ENABLE();
    CAN_GPIO_CLK_EN();

    uint32_t can_clk     = HAL_RCC_GetPCLK1Freq();
    uint32_t prescaler   = can_clk / (CAN_TQ_PER_BIT * CAN_BITRATE);
    uint32_t actual_baud = prescaler ? can_clk / (CAN_TQ_PER_BIT * prescaler) : 0;
    if (prescaler == 0) can_fatal("prescaler (APB1 too slow for 125 kbit/s)");

    // Both pads take the same drive style: on the shared-wire config they sit on
    // one node, where an enabled push-pull output driver would fight the FPGA.
    GPIO_InitTypeDef gpio = {0};
    gpio.Pin       = CAN_TX_PIN;
    gpio.Mode      = TX_PIN_MODE;
    gpio.Pull      = GPIO_NOPULL;
    gpio.Speed     = GPIO_SPEED_FREQ_VERY_HIGH;
    gpio.Alternate = CAN_GPIO_AF;
    HAL_GPIO_Init(CAN_GPIO_PORT, &gpio);

    // Pull-up on RX: a CAN line idles recessive, so an undriven PB8 reads idle
    // instead of floating into noise, and bxCAN can still leave init mode (it
    // waits for 11 recessive bits on RX -- see the note in setup()). Irrelevant
    // when something really drives the line, and it makes a low reading mean
    // something: a real driver holding the bus dominant.
    gpio.Pin  = CAN_RX_PIN;
    gpio.Pull = GPIO_PULLUP;
    HAL_GPIO_Init(CAN_GPIO_PORT, &gpio);

    hcan1.Instance                  = CAN1;
    hcan1.Init.Mode                 = PUBLISH_MODE;
    hcan1.Init.AutoRetransmission   = AUTO_RETRANSMIT;
    hcan1.Init.Prescaler            = prescaler;
    hcan1.Init.SyncJumpWidth        = CAN_SJW;
    hcan1.Init.TimeSeg1             = CAN_SEG1;   // (1+14)/20 = 75% sample point
    hcan1.Init.TimeSeg2             = CAN_SEG2;
    hcan1.Init.TimeTriggeredMode    = DISABLE;
    hcan1.Init.AutoBusOff           = DISABLE;
    hcan1.Init.AutoWakeUp           = DISABLE;
    hcan1.Init.ReceiveFifoLocked    = DISABLE;   // a full FIFO overwrites, never blocks
    // Chronological mailbox order. This matters here: every frame carries the
    // same ID, and with ID priority the tie is broken by mailbox number, which
    // would let the sequence numbers leave the board out of order.
    hcan1.Init.TransmitFifoPriority = ENABLE;

    if (HAL_CAN_Init(&hcan1) != HAL_OK)  can_fatal("Init");
    if (!can_config_filter())            can_fatal("ConfigFilter");

    // bxCAN clears MSR.INAK -- actually leaves init mode -- only once it has seen
    // 11 consecutive recessive bits on the RX pin, and HAL_CAN_Start allows 10 ms
    // for that. HAL_FDCAN_Start never looked at RX, so this failure is new on the
    // F446 and always means the same thing: RX is not idle-recessive. LOOPBACK is
    // immune (internal feedback replaces the pin), so config A boots regardless;
    // configs B and C need a healthy bus before the first frame. Retry rather
    // than die, so plugging the wire in is enough to bring the board up.
    while (HAL_CAN_Start(&hcan1) != HAL_OK) {
        Serial.print("# CAN Start failed: no 11 recessive bits on RX (PB8 reads ");
        Serial.print(HAL_GPIO_ReadPin(CAN_GPIO_PORT, CAN_RX_PIN) == GPIO_PIN_SET ? "high" : "LOW");
        Serial.println("); retrying in 1 s");
        delay(1000);
    }

    Serial.print("# APB1 clock "); Serial.print(can_clk);
    Serial.print(" Hz, prescaler ");  Serial.print(prescaler);
    Serial.print(", baud ");          Serial.println(actual_baud);
    if (actual_baud != CAN_BITRATE) {
        Serial.print("# WARNING baud off target by ");
        Serial.println((int32_t)actual_baud - (int32_t)CAN_BITRATE);
    }
    Serial.print("# wiring: "); Serial.println(BUS_NAME);
    report_rx_wire();
}

// Enqueue one frame. Returns false if all three TX mailboxes are busy -- that is
// the STM32/bus saturation signal, and it is deliberately NOT retried here.
static bool can_send(uint32_t s) {
    if (HAL_CAN_GetTxMailboxesFreeLevel(&hcan1) == 0) return false;

    int32_t raw = SEQ_MIN + (int32_t)(s % (uint32_t)SEQ_SPAN);
    uint8_t data[4] = {
        (uint8_t)(raw >> 24), (uint8_t)(raw >> 16),
        (uint8_t)(raw >> 8),  (uint8_t)(raw)
    };

    CAN_TxHeaderTypeDef tx = {0};
    tx.StdId              = CAN_ID;
    tx.ExtId              = 0;
    tx.IDE                = CAN_ID_STD;
    tx.RTR                = CAN_RTR_DATA;
    tx.DLC                = 4;
    tx.TransmitGlobalTime = DISABLE;

    uint32_t mailbox = 0;
    return HAL_CAN_AddTxMessage(&hcan1, &tx, data, &mailbox) == HAL_OK;
}

static void report_stat(void) {
    // bxCAN has no HAL error-counter accessor; TEC/REC are fields of ESR.
    uint32_t esr = hcan1.Instance->ESR;
    Serial.print("STAT sent=");   Serial.print(sent_count);
    Serial.print(" failed=");     Serial.print(failed_count);
    Serial.print(" tec=");        Serial.print((esr & CAN_ESR_TEC) >> CAN_ESR_TEC_Pos);
    Serial.print(" rec=");        Serial.println((esr & CAN_ESR_REC) >> CAN_ESR_REC_Pos);
}

static void handle_line(char *cmd) {
    if (strncmp(cmd, "RATE ", 5) == 0) {
        long fps = atol(cmd + 5);
        if (fps <= 0 || fps > 20000) {
            Serial.println("ERR rate out of range (1..20000)");
            return;
        }
        interval_us  = (uint32_t)(1000000UL / (uint32_t)fps);
        seq          = 0;
        sent_count   = 0;
        failed_count = 0;
        next_due_us  = micros();
        running      = true;
        Serial.print("OK RATE "); Serial.println(fps);

    } else if (strcmp(cmd, "STOP") == 0) {
        running = false;
        Serial.println("OK STOP");

    } else if (strcmp(cmd, "STAT") == 0) {
        report_stat();

    } else if (strcmp(cmd, "PING") == 0) {
        Serial.println("PONG");

    } else if (cmd[0] != '\0') {
        Serial.println("ERR unknown command");
    }
}

static void poll_serial(void) {
    while (Serial.available()) {
        char c = (char)Serial.read();
        if (c == '\n' || c == '\r') {
            if (line_len) {
                line[line_len] = '\0';
                handle_line(line);
                line_len = 0;
            }
        } else if (line_len < sizeof(line) - 1) {
            line[line_len++] = c;
        }
    }
}

void setup() {
    Serial.begin(115200);
    delay(300);
    Serial.println("# SCIS CAN load generator");
    can_init();
    Serial.println("# ready -- commands: RATE <fps> | STOP | STAT | PING");
}

void loop() {
    poll_serial();
    if (!running) return;

    // Wrap-safe deadline compare: micros() rolls over every ~71 minutes, and a
    // plain `now >= next` would stall the generator for a full wrap when it does.
    uint32_t now = micros();
    if ((int32_t)(now - next_due_us) < 0) return;

    if (can_send(seq)) sent_count++;
    else               failed_count++;
    seq++;

    next_due_us += interval_us;

    // If we have fallen far behind (host asked for more than the bus can carry),
    // resynchronise instead of trying to catch up in a burst -- a catch-up burst
    // would make the achieved rate spiky and unmeasurable.
    if ((int32_t)(micros() - next_due_us) > (int32_t)(10 * interval_us)) {
        next_due_us = micros() + interval_us;
    }
}
