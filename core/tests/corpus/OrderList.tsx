import React, { useCallback, useEffect, useState } from 'react';

export interface Props {
  orders: Order[]; /*?none*/
  onSelect: (id: string) => void;
}

const LIMIT = /{[a-z]+}/g; /*?none*/

/*<OrderList*/ export function OrderList({ orders, onSelect }: Props) {
  const [filter, setFilter] = useState('');
  const label = `Orders (${orders.length}) ${filter ? `for "${filter}"` : ''}`;

  useEffect(() => {
    if (filter.length > 3) {
      document.title = label; /*?OrderList*/
    }
  }, [filter]);

  const handleChange = useCallback((event) => {
    setFilter(event.target.value); /*?OrderList*/
  }, []);

  /*<handleSubmit*/ const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    const response = await fetch(`/api/orders?filter=${filter}`);
    if (!response.ok) {
      throw new Error('failed'); /*?handleSubmit*/
    }
  }; /*>handleSubmit*/

  const reset = () => {
    setFilter(''); /*?OrderList*/
  };

  return (
    <div>
      <p>Don't panic: {orders.length} orders</p> /*?OrderList*/
      <input value={filter} onChange={handleChange} />
      {orders.map((order) => (
        <button key={order.id} onClick={() => onSelect(order.id)}>
          {order.name}
        </button>
      ))}
      <button onClick={reset}>Reset</button>
    </div>
  );
} /*>OrderList*/

/*<OrderRow*/ export const OrderRow: React.FC<RowProps> = ({ order, selected }) => {
  const style = { fontWeight: selected ? 'bold' : 'normal' };
  const total = order.lines.reduce((sum, line) => sum + line.price, 0);
  return <li style={style}>{order.name}: {total}</li>; /*?OrderRow*/
}; /*>OrderRow*/

/*<identity*/ export const identity = <T,>(value: T): T => {
  const copy = value;
  const same = copy;
  const again = same;
  return again; /*?identity*/
}; /*>identity*/

/*<parse*/ export default async function parse(input: string): Promise<number> {
  const trimmed = input.trim();
  switch (trimmed) {
    case 'a': {
      return 1; /*?parse*/
    }
    default:
      return 0;
  }
} /*>parse*/

/*<gen*/ export async function* gen(limit: number) {
  let index = 0;
  while (index < limit) {
    yield index; /*?gen*/
    index++;
  }
} /*>gen*/

export const inline = (a: number) => ({ a }); /*?none*/

describe('OrderList', () => {
  it('renders', () => {
    expect(true).toBe(true); /*?none*/
  });
});
