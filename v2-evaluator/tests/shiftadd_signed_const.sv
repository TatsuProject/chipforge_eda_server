module top(input logic [3:0][7:0] res, input logic [5:0] c, output logic [7:0] y);
  assign y = res[(c * 4 + 3) % 4];
endmodule
