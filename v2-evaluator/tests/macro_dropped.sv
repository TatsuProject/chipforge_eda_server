(* blackbox *) module bb(input logic clk, input logic [9:0] a, output logic [127:0] q); endmodule
module top(input logic clk, input logic rst_n, input logic cap, input logic [31:0] cnt_ff, output logic [511:0] wsel_ff);
  logic [3:0][127:0] res;
  for (genvar k = 0; k < 4; k++) begin : g_out
    bb u_m (.clk(clk), .a(cnt_ff[9:0] ^ 10'(k)), .q(res[k]));
  end
  always_ff @(posedge clk or negedge rst_n) if (!rst_n) wsel_ff <= '0; else if (cap) wsel_ff <= {res[(cnt_ff[15:10] * 4 + 0) % 4], res[(cnt_ff[15:10] * 4 + 1) % 4],
                                          res[(cnt_ff[15:10] * 4 + 2) % 4], res[(cnt_ff[15:10] * 4 + 3) % 4]};
endmodule
